# Rebuilds certs/combined-ca-bundle.pem = certifi's public CAs + this machine's Windows roots.
# Exists because AVG Antivirus MITMs TLS on this machine and re-signs with a root that Windows
# trusts but certifi does not, so every Python HTTPS call fails CERTIFICATE_VERIFY_FAILED.
# Widening the trust bundle keeps verification on; disabling verification would not.
#
#   python certs/build_ca_bundle.py

from __future__ import annotations

import io
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
WINDOWS_ROOTS = os.path.join(HERE, "windows-roots.pem")
COMBINED = os.path.join(HERE, "combined-ca-bundle.pem")

# Reads the LocalMachine Root and CA stores and emits every certificate as PEM. Anything the
# operating system already trusts is in scope; nothing new is trusted by running this.
EXPORT = r"""
$sb = New-Object System.Text.StringBuilder
foreach ($store in @('Root','CA')) {
  $s = New-Object System.Security.Cryptography.X509Certificates.X509Store($store,'LocalMachine')
  $s.Open('ReadOnly')
  foreach ($c in $s.Certificates) {
    $b64 = [Convert]::ToBase64String($c.RawData, 'InsertLineBreaks')
    $null = $sb.AppendLine("# " + $c.Subject)
    $null = $sb.AppendLine("-----BEGIN CERTIFICATE-----")
    $null = $sb.AppendLine($b64)
    $null = $sb.AppendLine("-----END CERTIFICATE-----")
  }
  $s.Close()
}
[Console]::Out.Write($sb.ToString())
"""


def main():
    if sys.platform != "win32":
        print("Not Windows - certifi alone is almost certainly correct here. Nothing to do.")
        return 0

    import certifi

    result = subprocess.run(
        ["powershell", "-NoProfile", "-NonInteractive", "-Command", EXPORT],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        print(result.stderr[:500])
        return 1

    io.open(WINDOWS_ROOTS, "w", encoding="utf-8", newline="\n").write(result.stdout)
    base = io.open(certifi.where(), encoding="utf-8").read().rstrip()
    io.open(COMBINED, "w", encoding="utf-8", newline="\n").write(base + "\n" + result.stdout)

    total = io.open(COMBINED, encoding="utf-8").read().count("BEGIN CERTIFICATE")
    print(f"certifi {base.count('BEGIN CERTIFICATE')} + windows "
          f"{result.stdout.count('BEGIN CERTIFICATE')} = {total} certificates")
    print(f"wrote {os.path.relpath(COMBINED, os.path.dirname(HERE))}")
    print("agent/config.py points SSL_CERT_FILE at it automatically when it exists.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
