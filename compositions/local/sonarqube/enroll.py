"""Initial enrollment only. No scanner, policy mutation or deletion capability."""
import json
import os
from pathlib import Path
import re
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request


class EnrollmentError(Exception):
    """Only fixed, non-secret reason codes may escape the runner."""


def enroll(*, base, token_file, ca_file, app, uid, approvals_file):
    parsed = urllib.parse.urlsplit(base)
    if (not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,31}", app)
            or not re.fullmatch(r"[a-f0-9-]{36}", uid)
            or parsed.scheme != "https" or not parsed.hostname
            or parsed.username or parsed.password or parsed.query or parsed.fragment
            or parsed.path != "/sonarqube"):
        raise EnrollmentError("InvalidInput")
    token = Path(token_file).read_text().strip()
    if not token or any(c.isspace() for c in token):
        raise EnrollmentError("InvalidInput")

    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):
            return None

    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect(), urllib.request.HTTPSHandler(context=ssl.create_default_context(cafile=str(ca_file))))

    def once(path, data=None):
        req = urllib.request.Request(base + path, data=None if data is None else urllib.parse.urlencode(data).encode(),
                                     headers={"Authorization": "Bearer " + token, "Content-Type": "application/x-www-form-urlencoded"})
        try:
            with opener.open(req, timeout=10) as response:
                body = json.load(response)
                if not isinstance(body, dict):
                    raise EnrollmentError("MalformedResponse")
                return response.status, body
        except urllib.error.HTTPError as error:
            with error:
                try:
                    body = json.load(error)
                except (ValueError, UnicodeError):
                    body = {}
            return error.code, body
        except (ValueError, UnicodeError):
            raise EnrollmentError("MalformedResponse") from None
        except urllib.error.URLError as error:
            if isinstance(error.reason, ssl.SSLError):
                raise EnrollmentError("TlsFailed") from None
            return 0, {}
        except (TimeoutError, ConnectionError):
            return 0, {}

    def request(path, data=None):
        # GETs only: 3 attempts x 10s timeout, 1s then 2s backoff.
        # Never replay POST after transport errors, 429, 5xx or duplicate responses.
        for attempt in range(3 if data is None else 1):
            status, body = once(path, data)
            if data is not None or status not in (0, 429, 500, 502, 503, 504) or attempt == 2:
                return status, body
            time.sleep(attempt + 1)

    def get(path):
        status, body = request(path)
        if status != 200:
            raise EnrollmentError("ReadFailed")
        return body

    if get("/api/system/status").get("status") != "UP":
        raise EnrollmentError("ApiNotUp")
    if get("/api/authentication/validate").get("valid") is not True:
        raise EnrollmentError("AuthenticationFailed")
    if get("/api/users/current").get("login") != "digiorg-provisioner":
        raise EnrollmentError("ProvisionerIdentityMismatch")
    gates = get("/api/qualitygates/list").get("qualitygates", [])
    default_gates = [g for g in gates if g.get("isDefault") is True]
    if len(default_gates) != 1 or default_gates[0].get("name") != "Sonar way" or default_gates[0].get("isBuiltIn") is not True:
        raise EnrollmentError("QualityGateDefaultMismatch")
    profiles = get("/api/qualityprofiles/search").get("profiles", [])
    defaults = [p for p in profiles if p.get("isDefault") is True]
    if not defaults or any(p.get("name") != "Sonar way" or p.get("isBuiltIn") is not True for p in defaults):
        raise EnrollmentError("QualityProfileDefaultMismatch")
    key = "digiorg:gitea:DigiOrg:" + app
    lookup = "/api/components/show?" + urllib.parse.urlencode({"component": key})
    status, body = request(lookup)
    if status == 404:
        if body != {"errors": [{"msg": "Component key '%s' not found" % key}]}:
            raise EnrollmentError("UnverifiedAbsence")
        try:
            code, result = request("/api/projects/create", {"project": key, "name": app, "visibility": "private", "mainBranch": "main"})
        except EnrollmentError:
            code, result = 0, {}
        if code != 200 or result.get("project", {}).get("key") != key:
            # Read-only recovery documents the deterministic key; a successful
            # lookup alone does not prove who created a colliding project.
            observed_code, _ = request(lookup)
            raise EnrollmentError("AdoptionRequired" if observed_code == 200 else "CreateUnconfirmed")
    elif status == 200:
        approvals = json.loads(Path(approvals_file).read_text()) if Path(approvals_file).is_file() else []
        approved = [a for a in approvals if isinstance(a, dict) and a.get("key") == key and a.get("xrUID") == uid
                    and a.get("approvedBy") == "Chris" and re.fullmatch(r"https://github.com/digiorg/core/issues/372#issuecomment-[0-9]+", a.get("approvalRef", ""))]
        if len(approved) != 1:
            raise EnrollmentError("AdoptionRequired")
    else:
        raise EnrollmentError("LookupFailed")
    project_query = "?" + urllib.parse.urlencode({"project": key})
    gate = get("/api/qualitygates/get_by_project" + project_query).get("qualityGate", {})
    project_profiles = get("/api/qualityprofiles/search" + project_query).get("profiles", [])
    if (gate.get("name") != "Sonar way" or gate.get("default") is not True or not project_profiles
            or any(p.get("name") != "Sonar way" or p.get("isBuiltIn") is not True for p in project_profiles)):
        raise EnrollmentError("ProjectPolicyMismatch")
    component = get(lookup).get("component", {})
    if component.get("key") != key or component.get("qualifier") != "TRK" or component.get("visibility") != "private":
        raise EnrollmentError("IdentityMismatch")
    print("Enrolled " + key)


if __name__ == "__main__":
    try:
        enroll(base="https://digiorg.local/sonarqube", token_file="/var/run/sonarqube/token",
               ca_file="/var/run/digiorg-ca/ca.crt", app=os.environ["APP_NAME"], uid=os.environ["XR_UID"],
               approvals_file="/var/run/adoptions/approvals.json")
    except EnrollmentError as error:
        print("EnrollmentFailed " + str(error))
        raise SystemExit(1)
    except Exception:
        print("EnrollmentFailed InvalidInputOrTransport")
        raise SystemExit(1)
