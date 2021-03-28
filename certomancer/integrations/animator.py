import enum
import logging
import os
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

import tzlocal
from asn1crypto import ocsp, tsp, pem
from werkzeug.wrappers import Request, Response
from werkzeug.routing import Map, Rule, BaseConverter
from werkzeug.exceptions import HTTPException, NotFound, InternalServerError

from certomancer.registry import (
    PKIArchitecture, ServiceRegistry, ServiceLabel, CertLabel,
    CertomancerObjectNotFoundError, CertomancerConfig, ArchLabel
)
from certomancer.services import CertomancerServiceError


logger = logging.getLogger(__name__)


class LabelConverter(BaseConverter):
    regex = "[^./]+"


class ServiceType(enum.Enum):
    OCSP = 'ocsp'
    CRL_REPO = 'crl'
    TSA = 'tsa'
    CERT_REPO = 'certs'

    def endpoint(self, label: ServiceLabel):
        return Endpoint(self, label)


@dataclass(frozen=True)
class Endpoint:
    service_type: ServiceType
    label: ServiceLabel


def service_rules(services: ServiceRegistry):
    srv = ServiceType.OCSP
    for ocsp_info in services.list_ocsp_responders():
        logger.info("OCSP:" + ocsp_info.internal_url)
        yield Rule(
            ocsp_info.internal_url, endpoint=srv.endpoint(ocsp_info.label),
            methods=('POST',)
        )
    srv = ServiceType.TSA
    for tsa_info in services.list_time_stamping_services():
        logger.info("TSA:" + tsa_info.internal_url)
        yield Rule(
            tsa_info.internal_url, endpoint=srv.endpoint(tsa_info.label),
            methods=('POST',)
        )
    srv = ServiceType.CRL_REPO
    for crl_repo in services.list_crl_repos():
        logger.info("CRLs:" + crl_repo.internal_url)
        # latest CRL
        endpoint = srv.endpoint(crl_repo.label)
        yield Rule(
            f"{crl_repo.internal_url}/latest.<extension>",
            defaults={'crl_no': None}, endpoint=endpoint,
            methods=('GET',)
        )
        # CRL archive
        yield Rule(
            f"{crl_repo.internal_url}/archive-<int:crl_no>.<extension>",
            endpoint=endpoint, methods=('GET',)
        )
    srv = ServiceType.CERT_REPO
    for cert_repo in services.list_cert_repos():
        publish_issued = cert_repo.publish_issued_certs
        logger.info(
            f"CERT:{cert_repo.internal_url} "
            f"({'all certs' if publish_issued else 'CA only'})"
        )
        endpoint = srv.endpoint(cert_repo.label)
        yield Rule(
            f"{cert_repo.internal_url}/ca.<extension>",
            defaults={'cert_label': None}, endpoint=endpoint, methods=('GET',)
        )
        if publish_issued:
            yield Rule(
                f"{cert_repo.internal_url}/issued/"
                f"<label:cert_label>.<extension>",
                endpoint=endpoint, methods=('GET',)
            )


class Animator:

    def __init__(self, pki_arch: PKIArchitecture,
                 at_time: Optional[datetime] = None):
        self.pki_arch = pki_arch
        self.fixed_time = at_time

        self.url_map = Map(
            list(service_rules(pki_arch.service_registry)),
            converters={'label': LabelConverter}
        )

    @property
    def at_time(self):
        return self.fixed_time or datetime.now(tz=tzlocal.get_localzone())

    def serve_ocsp_response(self, request: Request, *, label: ServiceLabel):
        ocsp_resp = self.pki_arch.service_registry.summon_responder(
            label, self.at_time
        )
        data = request.stream.read()
        req: ocsp.OCSPRequest = ocsp.OCSPRequest.load(data)
        response = ocsp_resp.build_ocsp_response(req)
        return Response(response.dump(), mimetype='application/ocsp-response')

    def serve_timestamp_response(self, request, *, label: ServiceLabel):
        tsa = self.pki_arch.service_registry.summon_timestamper(
            label, self.at_time
        )
        data = request.stream.read()
        req: tsp.TimeStampReq = tsp.TimeStampReq.load(data)
        response = tsa.request_tsa_response(req)
        return Response(response.dump(), mimetype='application/timestamp-reply')

    def serve_crl(self, *, label: ServiceLabel, crl_no, extension):
        if extension == 'crl.pem':
            use_pem = True
            mime = 'application/x-pem-file'
        elif extension == 'crl':
            use_pem = False
            mime = 'application/pkix-crl'
        else:
            raise NotFound()

        if crl_no is not None:
            crl = self.pki_arch.service_registry.get_crl(label, number=crl_no)
        else:
            crl = self.pki_arch.service_registry.get_crl(label, self.at_time)

        data = crl.dump()
        if use_pem:
            data = pem.armor('X509 CRL', data)
        return Response(data, mimetype=mime)

    def serve_cert(self, *, label: ServiceLabel, cert_label: Optional[str],
                   extension):
        if extension == 'cert.pem':
            use_pem = True
            mime = 'application/x-pem-file'
        elif extension == 'crt':
            use_pem = False
            mime = 'application/pkix-cert'
        else:
            raise NotFound()

        cert_label = CertLabel(cert_label) if cert_label is not None else None

        cert = self.pki_arch.service_registry.get_cert_from_repo(
            label, cert_label
        )
        if cert is None:
            raise NotFound()

        data = cert.dump()
        if use_pem:
            data = pem.armor('certificate', data)
        return Response(data, mimetype=mime)

    def dispatch(self, request: Request):
        adapter = self.url_map.bind_to_environ(request.environ)
        # TODO even though this is a testing tool, inserting some safeguards
        #  to check request size etc. might be prudent
        try:
            endpoint, values = adapter.match()
            assert isinstance(endpoint, Endpoint)
            if endpoint.service_type == ServiceType.OCSP:
                return self.serve_ocsp_response(request, label=endpoint.label)
            if endpoint.service_type == ServiceType.TSA:
                return self.serve_timestamp_response(
                    request, label=endpoint.label
                )
            if endpoint.service_type == ServiceType.CRL_REPO:
                return self.serve_crl(label=endpoint.label, **values)
            if endpoint.service_type == ServiceType.CERT_REPO:
                return self.serve_cert(label=endpoint.label, **values)
            raise InternalServerError()  # pragma: nocover
        except CertomancerObjectNotFoundError as e:
            logger.info(e)
            return NotFound()
        except CertomancerServiceError as e:
            logger.error(e)
            return InternalServerError()
        except HTTPException as e:
            return e

    def __call__(self, environ, start_response):
        request = Request(environ)
        resp = self.dispatch(request)
        return resp(environ, start_response)


class LazyAnimator:
    def __init__(self):
        self.animator = None

    def _load(self):
        if self.animator is not None:
            return
        env = os.environ
        cfg_file = env['CERTOMANCER_CONFIG']
        key_dir = env['CERTOMANCER_KEY_DIR']
        arch = env['CERTOMANCER_ARCH']
        cfg = CertomancerConfig.from_file(cfg_file, key_dir)
        pki_arch = cfg.get_pki_arch(ArchLabel(arch))
        self.animator = Animator(pki_arch)

    def __call__(self, environ, start_response):
        self._load()
        return self.animator(environ, start_response)


app = LazyAnimator()
