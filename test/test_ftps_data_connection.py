"""File listing over FTPS when the printer is on another subnet from OctoPrint (issue #18).

A Bambu printer answers PASV with an address that is no use to the client: 0.0.0.0, or its own idea of its
address. The data connection has to go to the host the control connection is already on, which is what curl does
("Skip 0.0.0.0 for data connection, re-use <host> instead"). Python does that by itself only from 3.9.3 / 3.10
(ftplib's trust_server_pasv_ipv4_address); the Pythons before, which OctoPi images still ship, connect to the
address in the reply and the listing fails.

The stand-in printer below is an implicit-TLS FTP server that answers PASV the way the printer does.
"""

from __future__ import annotations

import importlib.util
import socket
import ssl
import subprocess
import sys
import threading
from pathlib import Path

import pytest

# Loaded from its file: importing the plugin's package would need OctoPrint itself.
_spec = importlib.util.spec_from_file_location(
    "ftps_client",
    Path(__file__).parent.parent / "octoprint_bambu_printer/printer/file_system/ftps_client.py",
)
ftps_client = importlib.util.module_from_spec(_spec)
sys.modules["ftps_client"] = ftps_client  # its dataclasses look their own module up by name
_spec.loader.exec_module(ftps_client)

FILES = ["benchy.3mf", "bracket.gcode.3mf"]


class StandInPrinter:
    """An implicit-TLS FTP server, good for one login and its listings, that reports `pasv_address` for PASV."""

    def __init__(self, certificate: tuple[str, str], pasv_address: str):
        self.context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        self.context.load_cert_chain(*certificate)
        self.pasv_address = pasv_address
        self.control = socket.create_server(("127.0.0.1", 0))
        self.port = self.control.getsockname()[1]
        threading.Thread(target=self._serve, daemon=True).start()

    def _serve(self):
        raw, _ = self.control.accept()
        conn = self.context.wrap_socket(raw, server_side=True)
        lines = conn.makefile("r", newline="\r\n")
        data_listener = None

        def say(text):
            conn.sendall((text + "\r\n").encode())

        say("220 stand-in printer")
        for line in lines:
            command, _, _argument = line.strip().partition(" ")
            if command in ("USER",):
                say("331 password please")
            elif command in ("PASS",):
                say("230 logged in")
            elif command in ("PBSZ", "PROT", "TYPE"):
                say("200 ok")
            elif command == "EPSV":
                say("502 not implemented")  # as the printer answers it
            elif command == "PASV":
                data_listener = socket.create_server(("127.0.0.1", 0))
                port = data_listener.getsockname()[1]
                say(f"227 ({self.pasv_address.replace('.', ',')},{port >> 8},{port & 255})")
            elif command in ("NLST", "LIST"):
                say("150 here it comes")
                data_listener.settimeout(5)
                try:
                    data_raw, _ = data_listener.accept()
                except socket.timeout:
                    say("425 nobody connected for the data")
                    continue
                data = self.context.wrap_socket(data_raw, server_side=True)
                data.sendall("".join(name + "\r\n" for name in FILES).encode())
                data.unwrap().close()
                say("226 done")
            elif command == "QUIT":
                say("221 bye")
                return
            else:
                say("500 what")


@pytest.fixture(scope="module")
def certificate(tmp_path_factory):
    folder = tmp_path_factory.mktemp("cert")
    cert, key = str(folder / "cert.pem"), str(folder / "key.pem")
    made = subprocess.run(
        ["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-keyout", key, "-out", cert,
         "-days", "1", "-subj", "/CN=printer"],
        capture_output=True,
    )
    if made.returncode != 0:
        pytest.skip("openssl isn't available to make the stand-in printer's certificate")
    return cert, key


def listing(port: int) -> list[str]:
    client = ftps_client.IoTFTPSClient("127.0.0.1", port, "bblp", "12345678", ssl_implicit=True)
    with client as connection:
        return sorted(path.name for path in connection.list_files("/"))


@pytest.mark.parametrize("reported", ["0.0.0.0", "10.99.99.99"])
def test_files_are_listed_whatever_address_the_printer_reports(certificate, reported, monkeypatch):
    # As a Python before 3.9.3 behaves: it goes where the reply says.
    monkeypatch.setattr(ftps_client.ftplib.FTP, "trust_server_pasv_ipv4_address", True, raising=False)
    socket.setdefaulttimeout(5)  # an address nothing answers on must fail the test, not hang it
    try:
        printer = StandInPrinter(certificate, pasv_address=reported)
        assert listing(printer.port) == sorted(FILES)
    finally:
        socket.setdefaulttimeout(None)


def test_files_are_listed_on_a_current_python_too(certificate):
    printer = StandInPrinter(certificate, pasv_address="0.0.0.0")
    assert listing(printer.port) == sorted(FILES)
