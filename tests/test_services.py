import importlib
from datetime import datetime

import pytz
from freezegun import freeze_time
from pyhanko_certvalidator import ValidationContext, CertificateValidator

from certomancer.integrations import illusionist
from certomancer.registry import CertomancerConfig, ArchLabel, ServiceLabel, \
    CertLabel

importlib.import_module('certomancer.default_plugins')

CONFIG = CertomancerConfig.from_file(
    'tests/data/with-services.yml', 'tests/data'
)

ARCH = CONFIG.get_pki_arch(ArchLabel('testing-ca'))

ILLUSIONIST = illusionist.Illusionist(pki_arch=ARCH)


def _check_crl_cardinality(crl, expected_revoked):
    assert len(crl['tbs_cert_list']['revoked_certificates']) == expected_revoked


def test_crl():
    some_crl = ARCH.service_registry.get_crl(
        ServiceLabel('interm'),
        at_time=datetime.fromisoformat('2020-11-01 00:00:00+00:00'),
    )
    _check_crl_cardinality(some_crl, expected_revoked=0)
    some_crl2 = ARCH.service_registry.get_crl(
        ServiceLabel('interm'),
        at_time=datetime.fromisoformat('2020-12-02 00:00:00+00:00'),
    )
    _check_crl_cardinality(some_crl2, expected_revoked=0)
    some_crl3 = ARCH.service_registry.get_crl(
        ServiceLabel('interm'),
        at_time=datetime.fromisoformat('2020-12-29 00:00:00+00:00'),
    )
    _check_crl_cardinality(some_crl3, expected_revoked=1)
    revo = some_crl3['tbs_cert_list']['revoked_certificates'][0]
    rev_time = datetime(2020, 12, 1, tzinfo=pytz.utc)
    assert revo['revocation_date'].native == rev_time

    reason = next(
        ext['extn_value'].native for ext in revo['crl_entry_extensions']
        if ext['extn_id'].native == 'crl_reason'
    )
    assert reason == 'key_compromise'


def test_aia_ca_issuers():
    signer1 = ARCH.get_cert(CertLabel('signer1'))
    ca_issuer_urls = {
        aia_entry['access_location']
        for aia_entry
        in signer1.authority_information_access_value.native
        if aia_entry['access_method'] == 'ca_issuers'
    }
    assert ca_issuer_urls == {
        'http://test.test/testing-ca/certs/interm/ca.crt',
        'http://test.test/testing-ca/certs/root/issued/interm.crt'
    }


@freeze_time('2020-11-01')
def test_validate(requests_mock):
    ILLUSIONIST.register(requests_mock)
    signer_cert = ARCH.get_cert(CertLabel('signer1'))
    root = ARCH.get_cert(CertLabel('root'))
    interm = ARCH.get_cert(CertLabel('interm'))
    vc = ValidationContext(
        trust_roots=[root], allow_fetching=True,
        revocation_mode='hard-fail', other_certs=[interm]
    )

    validator = CertificateValidator(
        signer_cert, intermediate_certs=[], validation_context=vc
    )
    validator.validate_usage({'digital_signature'})

    assert len(vc.ocsps)
    assert len(vc.crls)