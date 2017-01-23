# -*- coding: utf-8 -*-


import optparse
from xml.etree.ElementTree import Element

from nassl._nassl import OpenSSLError

from nassl import X509_NAME_MISMATCH, X509_NAME_MATCHES_SAN, X509_NAME_MATCHES_CN
from nassl.ocsp_response import OcspResponse
from nassl.ssl_client import ClientCertificateRequested
from nassl.x509_certificate import X509Certificate
from sslyze.plugins import plugin_base
from sslyze.plugins.plugin_base import PluginResult, ScanCommand
from sslyze.plugins.utils.certificate import Certificate
from sslyze.plugins.utils.trust_store.trust_store import TrustStore, \
    InvalidCertificateChainOrderError, AnchorCertificateNotInTrustStoreError
from sslyze.plugins.utils.trust_store.trust_store_repository import TrustStoresRepository
from sslyze.server_connectivity import ServerConnectivityInfo
from sslyze.utils.thread_pool import ThreadPool
from typing import Dict
from typing import List
from typing import Optional
from typing import Tuple


class PathValidationResult(object):
    """The result of trying to validate a server's certificate chain using a specific trust store.
    """
    def __init__(self, trust_store, verify_string):
        # type: (TrustStore, unicode) -> None
        # The trust store used for validation
        self.trust_store = trust_store

        # The string returned by OpenSSL's validation function
        self.verify_string = verify_string
        self.is_certificate_trusted = True if verify_string == 'ok' else False


class PathValidationError(object):
    """An exception was raised while trying to validate a server's certificate using a specific trust store; should
    never happen.
    """
    def __init__(self, trust_store, exception):
        # type: (TrustStore, Exception) -> None
        self.trust_store = trust_store
        # Cannot keep the full exception as it may not be pickable (ie. _nassl.OpenSSLError)
        self.error_message = '{} - {}'.format(str(exception.__class__.__name__), str(exception))


class CertificateInfoScanCommand(ScanCommand):
    """Verify the validity of the server(s) certificate(s) against various trust stores and checks for OCSP stapling
    support.
    """

    def __init__(self, ca_file=None, print_full_certificate=False):
        # type: (Optional[str], Optional[bool]) -> None
        super(CertificateInfoScanCommand, self).__init__()
        self.custom_ca_file = ca_file
        self.should_print_full_certificate = print_full_certificate

    @classmethod
    def get_cli_argument(cls):
        return u'certinfo'

    @classmethod
    def get_plugin_class(cls):
        return CertificateInfoPlugin


class CertificateInfoPlugin(plugin_base.Plugin):
    """Retrieve and validate the server(s)' certificate chain.
    """

    @classmethod
    def get_available_commands(cls):
        return [CertificateInfoScanCommand]

    @classmethod
    def get_cli_option_group(cls):
        options = super(CertificateInfoPlugin, cls).get_cli_option_group()

        # Add the special optional argument for this plugin's commands
        # They must match the names in the commands' contructor
        options.append(
            optparse.make_option(
                u'--ca_file',
                help=u'Local Certificate Authority file (in PEM format), to verify the validity of the server(s)'
                     u' certificate(s) against.',
                action=u'store_true'
            )
        )
        # TODO(ad): Move this to the command line parser ?
        options.append(
            optparse.make_option(
                u'--print_full_certificate',
                help=u'Option - Print the full content of server certificate instead of selected fields.',
                action=u'store_true'
            )
        )
        return options


    def process_task(self, server_info, scan_command):
        # type: (ServerConnectivityInfo, CertificateInfoScanCommand) -> CertificateInfoResult
        final_trust_store_list = list(TrustStoresRepository.get_all())
        if scan_command.custom_ca_file:
            final_trust_store_list.append(TrustStore(scan_command.custom_ca_file, u'Custom --ca_file', u'N/A'))

        thread_pool = ThreadPool()
        for trust_store in final_trust_store_list:
            # Try to connect with each trust store
            thread_pool.add_job((self._get_and_verify_certificate_chain, (server_info, trust_store)))

        # Start processing the jobs; one thread per trust
        thread_pool.start(len(final_trust_store_list))

        # Store the results as they come
        certificate_chain = []
        path_validation_result_list = []
        path_validation_error_list = []
        ocsp_response = None

        for (job, result) in thread_pool.get_result():
            (_, (_, trust_store)) = job
            certificate_chain, validation_result, ocsp_response = result
            # Store the returned verify string for each trust store
            path_validation_result_list.append(PathValidationResult(trust_store, validation_result))

        # Store thread pool errors
        last_exception = None
        for (job, exception) in thread_pool.get_error():
            (_, (_, trust_store)) = job
            path_validation_error_list.append(PathValidationError(trust_store, exception))
            last_exception = exception

        thread_pool.join()

        if len(path_validation_error_list) == len(final_trust_store_list):
            # All connections failed unexpectedly; raise an exception instead of returning a result
            raise RuntimeError(u'Could not connect to the server; last error: {}'.format(last_exception))

        # All done
        return CertificateInfoResult(server_info, scan_command, certificate_chain, path_validation_result_list,
                            path_validation_error_list, ocsp_response)


    @staticmethod
    def _get_and_verify_certificate_chain(server_info, trust_store):
        # type: (ServerConnectivityInfo, TrustStore) -> Tuple[X509Certificate, unicode, OcspResponse]
        """Connects to the target server and uses the supplied trust store to validate the server's certificate.
        Returns the server's certificate and OCSP response.
        """
        ssl_connection = server_info.get_preconfigured_ssl_connection(ssl_verify_locations=trust_store.path)

        # Enable OCSP stapling
        ssl_connection.set_tlsext_status_ocsp()

        try:  # Perform the SSL handshake
            ssl_connection.connect()

            ocsp_response = ssl_connection.get_tlsext_status_ocsp_resp()
            x509_cert_chain = ssl_connection.get_peer_cert_chain()
            (_, verify_str) = ssl_connection.get_certificate_chain_verify_result()

        except ClientCertificateRequested:  # The server asked for a client cert
            # We can get the server cert anyway
            ocsp_response = ssl_connection.get_tlsext_status_ocsp_resp()
            x509_cert_chain = ssl_connection.get_peer_cert_chain()
            (_, verify_str) = ssl_connection.get_certificate_chain_verify_result()

        finally:
            ssl_connection.close()

        return x509_cert_chain, verify_str, ocsp_response


class CertificateInfoResult(PluginResult):
    """The result of running a CertificateInfoScanCommand on a specific server.

    Attributes:
        certificate_chain (List[Certificate]): The certificate chain sent by the server; index 0 is the leaf
            certificate.
        verified_certificate_chain (List[Certificate]): The verified certificate chain; index 0 is the leaf
            certificate and the last element is the anchor/CA certificate from the Mozilla trust store. Will be empty if
            validation failed or the verified chain could not be built.
        is_leaf_certificate_ev (bool): True if the leaf certificate is Extended Validation according to Mozilla.
        path_validation_result_list (List[PathValidationResult]): A list of attempts at validating the server's
            certificate chain path using various trust stores.
        path_validation_error_list (List[PathValidationError]):  A list of attempts at validating the server's
            certificate chain path that triggered an unexpected error.
        hostname_validation_result (int): Validation result of the certificate hostname.
        ocsp_response (Optional[dict]): The OCSP response returned by the server.
        is_ocsp_response_trusted (Optional[bool]): True if the OCSP response is trusted using the Mozilla trust store.
        has_sha1_in_certificate_chain (bool): True if any of the leaf or intermediate certificates are signed using the
            SHA-1 algorithm. None if the verified chain could not be built or no HPKP header was returned.
        has_anchor_in_certificate_chain (bool): True if the server included the anchor/root certificate in the chain it
            send back to clients. None if the verified chain could not be built or no HPKP header was returned.
    """

    COMMAND_TITLE = u'Certificate Basic Information'

    def __init__(
            self,
            server_info,                    # type: ServerConnectivityInfo
            scan_command,                   # type: CertificateInfoScanCommand
            certificate_chain,              # type: List[X509Certificate]
            path_validation_result_list,    # type: List[PathValidationResult]
            path_validation_error_list,     # type: List[PathValidationError]
            ocsp_response                   # type: Optional[Dict]
            ):
        super(CertificateInfoResult, self).__init__(server_info, scan_command)

        main_trust_store = TrustStoresRepository.get_main()

        # We only keep the dictionary as a nassl.OcspResponse is not pickable
        self.ocsp_response = ocsp_response.as_dict() if ocsp_response else None
        print ocsp_response.verify(main_trust_store.path)
        self.is_ocsp_response_trusted = ocsp_response.verify(main_trust_store.path) if ocsp_response else False

        # We create pickable Certificates from nassl.X509Certificates which are not pickable
        self.certificate_chain = [Certificate.from_nassl(x509_cert) for x509_cert in certificate_chain]
        self.is_leaf_certificate_ev = main_trust_store.is_extended_validation(self.certificate_chain[0])

        # Try to build the verified chain
        self.verified_certificate_chain = []
        self.is_certificate_chain_order_valid = True
        try:
            self.verified_certificate_chain = main_trust_store.build_verified_certificate_chain(self.certificate_chain)
        except InvalidCertificateChainOrderError:
            self.is_certificate_chain_order_valid = False
        except AnchorCertificateNotInTrustStoreError:
            pass

        # Find the result of the validation with the main trust store (Mozilla)
        is_chain_trusted_by_main_store = False
        for path_result in path_validation_result_list:
            if path_result.trust_store == main_trust_store:
                is_chain_trusted_by_main_store = path_result.is_certificate_trusted

        if not is_chain_trusted_by_main_store:
            # Somehow we were able to build a verified chain but the validation failed - expired cert?
            self.verified_certificate_chain = []

        self.has_anchor_in_certificate_chain = None
        if self.verified_certificate_chain:
            self.has_anchor_in_certificate_chain = self.verified_certificate_chain[-1] in self.certificate_chain

        self.path_validation_result_list = path_validation_result_list
        self.path_validation_error_list = path_validation_error_list
        self.hostname_validation_result = certificate_chain[0].matches_hostname(server_info.tls_server_name_indication)

        # Check if a SHA1-signed certificate is in the chain
        # Root certificates can still be signed with SHA1 so we only check leaf and intermediate certificates
        self.has_sha1_in_certificate_chain = None
        if self.verified_certificate_chain:
            self.has_sha1_in_certificate_chain = False
            for cert in self.verified_certificate_chain[:-1]:
                if u"sha1" in cert.as_dict['signatureAlgorithm']:
                    self.has_sha1_in_certificate_chain = True
                    break


    HOSTNAME_VALIDATION_TEXT = {
        X509_NAME_MATCHES_SAN: u'OK - Subject Alternative Name matches {hostname}'.format,
        X509_NAME_MATCHES_CN: u'OK - Common Name matches {hostname}'.format,
        X509_NAME_MISMATCH: u'FAILED - Certificate does NOT match {hostname}'.format
    }

    TRUST_FORMAT = u'{store_name} CA Store ({store_version}):'.format

    NO_VERIFIED_CHAIN_ERROR_TXT = u'ERROR - Could not build verified chain (certificate untrusted?)'

    def as_text(self):
        text_output = [self._format_title(self.COMMAND_TITLE)]
        if self.scan_command.should_print_full_certificate:
            text_output.extend(self._get_full_certificate_text())
        else:
            text_output.extend(self._get_basic_certificate_text())

        # Trust section
        text_output.extend(['', self._format_title(u'Certificate - Trust')])

        # Hostname validation
        server_name_indication = self.server_info.tls_server_name_indication
        if self.server_info.tls_server_name_indication != self.server_info.hostname:
            text_output.append(self._format_field(u"SNI enabled with virtual domain:", server_name_indication))

        text_output.append(self._format_field(
                u"Hostname Validation:",
                self.HOSTNAME_VALIDATION_TEXT[self.hostname_validation_result](hostname=server_name_indication))
        )

        # Path validation that was successfully tested
        for path_result in self.path_validation_result_list:
            if path_result.is_certificate_trusted:
                # EV certs - Only Mozilla supported for now
                ev_txt = u''
                # TODO: Validate tath this works
                if self.is_leaf_certificate_ev and TrustStoresRepository.get_main() == path_result.trust_store:
                    ev_txt = u', Extended Validation'
                path_txt = u'OK - Certificate is trusted{}'.format(ev_txt)

            else:
                path_txt = u'FAILED - Certificate is NOT Trusted: {}'.format(path_result.verify_string)

            text_output.append(self._format_field(self.TRUST_FORMAT(store_name=path_result.trust_store.name,
                                                                    store_version=path_result.trust_store.version),
                                                  path_txt))

        # Path validation that ran into errors
        for path_error in self.path_validation_error_list:
            error_txt = u'ERROR: {}'.format(path_error.error_message)
            text_output.append(self._format_field(self.TRUST_FORMAT(store_name=path_result.trust_store.name,
                                                                    store_version=path_result.trust_store.version),
                                                  error_txt))

        # Print the Common Names within the certificate chain
        cns_in_certificate_chain = [cert.printable_subject_name for cert in self.certificate_chain]
        text_output.append(self._format_field(u'Received Chain:', u' --> '.join(cns_in_certificate_chain)))

        # Print the Common Names within the verified certificate chain if validation was successful
        if self.verified_certificate_chain:
            cns_in_certificate_chain = [cert.printable_subject_name for cert in self.verified_certificate_chain]
            verified_chain_txt = ' --> '.join(cns_in_certificate_chain)
        else:
            verified_chain_txt = self.NO_VERIFIED_CHAIN_ERROR_TXT
        text_output.append(self._format_field(u'Verified Chain w/ Mozilla Store:', verified_chain_txt))

        if self.verified_certificate_chain:
            chain_with_anchor_txt = u'OK - Anchor certificate not sent' if not self.has_anchor_in_certificate_chain \
                else u'WARNING - Received certificate chain contains the anchor certificate'
        else:
            chain_with_anchor_txt = self.NO_VERIFIED_CHAIN_ERROR_TXT
        text_output.append(self._format_field(u'Received Chain Contains Anchor:', chain_with_anchor_txt))

        chain_order_txt = u'OK - Order is valid' if self.is_certificate_chain_order_valid \
            else u'FAILED - Certificate chain out of order!'
        text_output.append(self._format_field(u'Received Chain Order:', chain_order_txt))

        if self.verified_certificate_chain:
            sha1_text = u'OK - No SHA1-signed certificate in the verified certificate chain' \
                if not self.has_sha1_in_certificate_chain \
                else u'INSECURE - SHA1-signed certificate in the verified certificate chain'
        else:
            sha1_text = self.NO_VERIFIED_CHAIN_ERROR_TXT

        text_output.append(self._format_field(u'Verified Chain contains SHA1:', sha1_text))

        # OCSP stapling
        text_output.extend(['', self._format_title(u'Certificate - OCSP Stapling')])

        if self.ocsp_response is None:
            text_output.append(self._format_field(u'', u'NOT SUPPORTED - Server did not send back an OCSP response.'))

        else:
            try:
                ocsp_trust_txt = u'OK - Response is trusted' \
                    if self.is_ocsp_response_trusted \
                    else u'FAILED - Response is NOT trusted'
            except OpenSSLError as e:
                if 'certificate verify error' in str(e):
                    ocsp_trust_txt = u'FAILED - Response is NOT trusted'
                else:
                    raise

            ocsp_resp_txt = [
                self._format_field(u'OCSP Response Status:', self.ocsp_response['responseStatus']),
                self._format_field(u'Validation w/ Mozilla Store:', ocsp_trust_txt),
                self._format_field(u'Responder Id:', self.ocsp_response['responderID'])]

            if 'successful' in self.ocsp_response['responseStatus']:
                ocsp_resp_txt.extend([
                    self._format_field(u'Cert Status:', self.ocsp_response['responses'][0]['certStatus']),
                    self._format_field(u'Cert Serial Number:', self.ocsp_response['responses'][0]['certID']['serialNumber']),
                    self._format_field(u'This Update:', self.ocsp_response['responses'][0]['thisUpdate']),
                    self._format_field(u'Next Update:', self.ocsp_response['responses'][0]['nextUpdate'])
                ])
            text_output.extend(ocsp_resp_txt)

        # All done
        return text_output


    def as_xml(self):
        xml_output = Element(self.scan_command.get_cli_argument(), title=self.COMMAND_TITLE)

        # Certificate chain
        cert_xml_list = []
        for index, certificate in enumerate(self.certificate_chain, start=0):
            cert_xml = Element('certificate', attrib={
                'sha1Fingerprint': certificate.sha1_fingerprint,
                'position': 'leaf' if index == 0 else 'intermediate',
                'suppliedServerNameIndication': self.server_info.tls_server_name_indication,
                'hpkpSha256Pin': certificate.hpkp_pin
            })

            # Add the PEM cert
            cert_as_pem_xml = Element('asPEM')
            cert_as_pem_xml.text = certificate.as_pem
            cert_xml.append(cert_as_pem_xml)

            # Add the parsed certificate
            for key, value in certificate.as_dict.items():
                cert_xml.append(_keyvalue_pair_to_xml(key, value))
            cert_xml_list.append(cert_xml)


        cert_chain_attrs ={'isChainOrderValid': str(self.is_certificate_chain_order_valid)}
        if self.verified_certificate_chain:
            cert_chain_attrs['containsAnchorCertificate'] = str(False) if not self.has_anchor_in_certificate_chain \
                else str(True)
        cert_chain_xml = Element('receivedCertificateChain', attrib=cert_chain_attrs)

        for cert_xml in cert_xml_list:
            cert_chain_xml.append(cert_xml)
        xml_output.append(cert_chain_xml)


        # Trust
        trust_validation_xml = Element('certificateValidation')

        # Hostname validation
        is_hostname_valid = 'False' if self.hostname_validation_result == X509_NAME_MISMATCH else 'True'
        host_validation_xml = Element('hostnameValidation', serverHostname=self.server_info.tls_server_name_indication,
                                      certificateMatchesServerHostname=is_hostname_valid)
        trust_validation_xml.append(host_validation_xml)

        # Path validation that was successful
        for path_result in self.path_validation_result_list:
            path_attrib_xml = {
                'usingTrustStore': path_result.trust_store.name,
                'trustStoreVersion': path_result.trust_store.version,
                'validationResult': path_result.verify_string
            }

            # Things we only do with the Mozilla store
            verified_cert_chain_xml = None
            if 'Mozilla' in path_result.trust_store.name:
                # EV certs
                if self.is_leaf_certificate_ev:
                    path_attrib_xml['isExtendedValidationCertificate'] = str(self.is_leaf_certificate_ev)

                # Verified chain
                if self.verified_certificate_chain:
                    verified_cert_chain_xml = Element(
                        'verifiedCertificateChain',
                        {'hasSha1SignedCertificate': str(self.has_sha1_in_certificate_chain)}
                    )
                    for certificate in self.certificate_chain:
                        cert_xml = Element('certificate', attrib={
                            'sha1Fingerprint': certificate.sha1_fingerprint,
                            'suppliedServerNameIndication': self.server_info.tls_server_name_indication,
                            'hpkpSha256Pin': certificate.hpkp_pin
                        })

                        # Add the PEM cert
                        cert_as_pem_xml = Element('asPEM')
                        cert_as_pem_xml.text = certificate.as_pem
                        cert_xml.append(cert_as_pem_xml)

                        # Add the parsed certificate
                        for key, value in certificate.as_dict.items():
                            cert_xml.append(_keyvalue_pair_to_xml(key, value))
                        cert_xml_list.append(cert_xml)

                        verified_cert_chain_xml.append(cert_xml)

            path_valid_xml = Element('pathValidation', attrib=path_attrib_xml)
            if verified_cert_chain_xml is not None:
                path_valid_xml.append(verified_cert_chain_xml)

            trust_validation_xml.append(path_valid_xml)


        # Path validation that ran into errors
        for path_error in self.path_validation_error_list:
            error_txt = 'ERROR: {}'.format(path_error.error_message)
            path_attrib_xml = {
                'usingTrustStore': path_result.trust_store.name,
                'trustStoreVersion': path_result.trust_store.version,
                'error': error_txt
            }

            trust_validation_xml.append(Element('pathValidation', attrib=path_attrib_xml))

        xml_output.append(trust_validation_xml)


        # OCSP Stapling
        ocsp_xml = Element('ocspStapling', attrib={'isSupported': 'False' if self.ocsp_response is None else 'True'})

        if self.ocsp_response:
            ocsp_resp_xmp = Element('ocspResponse',
                                    attrib={'isTrustedByMozillaCAStore': str(self.is_ocsp_response_trusted)})
            for (key, value) in self.ocsp_response.items():
                ocsp_resp_xmp.append(_keyvalue_pair_to_xml(key, value))

            ocsp_xml.append(ocsp_resp_xmp)
        xml_output.append(ocsp_xml)

        # All done
        return xml_output


    def _get_full_certificate_text(self):
        return [self.certificate_chain[0].as_text]


    def _get_basic_certificate_text(self):
        cert_dict = self.certificate_chain[0].as_dict

        text_output = [
            self._format_field(u"SHA1 Fingerprint:", self.certificate_chain[0].sha1_fingerprint),
            self._format_field(u"Common Name:", self.certificate_chain[0].printable_subject_name),
            self._format_field(u"Issuer:", self.certificate_chain[0].printable_issuer_name),
            self._format_field(u"Serial Number:", cert_dict['serialNumber']),
            self._format_field(u"Not Before:", cert_dict['validity']['notBefore']),
            self._format_field(u"Not After:", cert_dict['validity']['notAfter']),
            self._format_field(u"Signature Algorithm:", cert_dict['signatureAlgorithm']),
            self._format_field(u"Public Key Algorithm:", cert_dict['subjectPublicKeyInfo']['publicKeyAlgorithm']),
            self._format_field(u"Key Size:", cert_dict['subjectPublicKeyInfo']['publicKeySize'])]

        try:
            # Print the Public key exponent if there's one; EC public keys don't have one for example
            text_output.append(self._format_field(u"Exponent:", u"{0} (0x{0:x})".format(
                int(cert_dict['subjectPublicKeyInfo']['publicKey']['exponent']))))
        except KeyError:
            pass

        try:
            # Print the SAN extension if there's one
            text_output.append(self._format_field(u'X509v3 Subject Alternative Name:',
                                                  cert_dict['extensions']['X509v3 Subject Alternative Name']))
        except KeyError:
            pass

        return text_output


# XML generation
def _create_xml_node(key, value=''):
    key = key.replace(' ', '').strip()  # Remove spaces
    key = key.replace('/', '').strip()  # Remove slashes (S/MIME Capabilities)
    key = key.replace('<', '_')
    key = key.replace('>', '_')

    # Things that would generate invalid XML
    if key[0].isdigit():  # Tags cannot start with a digit
            key = 'oid-' + key

    xml_node = Element(key)
    xml_node.text = value.decode("utf-8").strip()
    return xml_node


def _keyvalue_pair_to_xml(key, value=''):

    if type(value) is str:  # value is a string
        key_xml = _create_xml_node(key, value)

    elif type(value) is int:
        key_xml = _create_xml_node(key, str(value))

    elif value is None:  # no value
        key_xml = _create_xml_node(key)

    elif type(value) is list:
        key_xml = _create_xml_node(key)
        for val in value:
            key_xml.append(_keyvalue_pair_to_xml('listEntry', val))

    elif type(value) is dict:  # value is a list of subnodes
        key_xml = _create_xml_node(key)
        for subkey in value.keys():
            key_xml.append(_keyvalue_pair_to_xml(subkey, value[subkey]))
    else:
        raise Exception()

    return key_xml

