#!/usr/bin/env python3

import datetime
import logging
import os
import re
import signal
import subprocess
import sys

import vrnetlab
from scrapli import Scrapli

STARTUP_CONFIG_FILE = "/config/startup-config.cfg"

# DAY0: hand the ASA its whole configuration on a CD-ROM, the way Cisco
# deploys ASAv on KVM, instead of typing it at the serial console. The
# console is 80 columns: any line much over fifty characters wraps, the
# ASA redraws it with backspaces, scrapli never matches a prompt again
# and the launcher times out and takes the container with it. And an HA
# pair typed in parallel forms part-way through, after which the new
# standby refuses the rest. From day0 both units boot fully configured,
# which is how a real pair powered up together behaves.
#
# Opt-in with ASAV_DAY0=1, so without it this image is unchanged.
DAY0_CONFIG_FILE = "/day0-config"
DAY0_ISO = "/day0.iso"
# End of boot on a day0 ASA: the banner before the first prompt, or an
# exec prompt - role first when failover is configured - NOT anchored at
# end of line, because failover prints progress dots after it.
DAY0_READY = (
    r"(?m)Type help or '\?' for a list of available commands"
    r"|^[\w.-]+(/[\w.-]+)*> "
)

# ASA has some password complexity requirements
ENABLE_PASSWORD = "CiscoAsa1!"


def handle_SIGCHLD(signal, frame):
    os.waitpid(-1, os.WNOHANG)


def handle_SIGTERM(signal, frame):
    sys.exit(0)


signal.signal(signal.SIGINT, handle_SIGTERM)
signal.signal(signal.SIGTERM, handle_SIGTERM)
signal.signal(signal.SIGCHLD, handle_SIGCHLD)

TRACE_LEVEL_NUM = 9
logging.addLevelName(TRACE_LEVEL_NUM, "TRACE")


def trace(self, message, *args, **kws):
    # Yes, logger takes its '*args' as 'args'.
    if self.isEnabledFor(TRACE_LEVEL_NUM):
        self._log(TRACE_LEVEL_NUM, message, args, **kws)


logging.Logger.trace = trace


class ASAv_vm(vrnetlab.VM):
    def __init__(self, username, password, conn_mode, hostname, install_mode=False):
        for e in os.listdir("/"):
            if re.search(".qcow2$", e):
                disk_image = "/" + e

        super(ASAv_vm, self).__init__(
            username,
            password,
            disk_image=disk_image,
            ram=2048,
            cpu="Nehalem",
        )
        self.hostname = hostname
        self.nic_type = "e1000"
        self.conn_mode = conn_mode
        self.install_mode = install_mode
        self.num_nics = 8

        self.day0 = (
            not install_mode
            and os.environ.get("ASAV_DAY0", "0") == "1"
            and os.path.exists(STARTUP_CONFIG_FILE)
        )
        if self.day0:
            with open(STARTUP_CONFIG_FILE, "r") as config:
                startup_config = config.read()
            # `console serial` first, as in Cisco's own KVM day0 example:
            # the launcher and every console tool here read ttyS0, and a
            # day0 ASAv is not guaranteed to keep its console there.
            self.create_day0_image(
                "console serial\n"
                + self.gen_bootstrap_config()
                + "\n"
                + startup_config
            )
            self.qemu_args.extend(["-cdrom", DAY0_ISO])

    def create_day0_image(self, config):
        """Write the config as `day0-config` at the root of an ISO."""
        with open(DAY0_CONFIG_FILE, "w") as cfg:
            cfg.write(config)
        self.logger.info("Generating day0 ISO (%d lines)", len(config.splitlines()))
        subprocess.run(
            ["genisoimage", "-r", "-l", "-o", DAY0_ISO, DAY0_CONFIG_FILE],
            check=True,
        )

    def bootstrap_spin(self):
        """This function should be called periodically to do work."""

        if self.spins > 300:
            # too many spins with no result ->  give up
            self.stop()
            self.start()
            return

        if self.day0:
            # Configured from day0, so there is nothing to type - only the
            # end of boot to recognise, and it MUST be recognised: until it
            # is, con_expect() is a BLOCKING read, and an hour of quiet
            # console raises ScrapliTimeout and the container exits.
            #
            # The marker is the banner the ASA prints once, straight before
            # its first prompt. Not the prompt itself: it carries the role
            # first (`pri/act/fw1>`) and, while a pair forms,
            # failover writes progress on the SAME line (`...> .`). The
            # buffer carries across reads, so a line split between two
            # reads is still seen.
            (ridx, match, res) = self.con_expect([b"\x00"], 1)
            if res != b"":
                self.write_to_stdout(res)
                self.spins = 0
            self.day0_seen = (getattr(self, "day0_seen", b"") + res)[-8192:]
            text = self.day0_seen.decode(errors="ignore")
            if re.search(DAY0_READY, text):
                startup_time = datetime.datetime.now() - self.start_time
                self.logger.info("day0 configuration applied at boot")
                self.logger.info("Startup complete in: %s" % startup_time)
                # Let go of the console, as the typed path does when it
                # finishes: it takes one client at a time, and holding it
                # locks out `telnet 127.0.0.1 5000` for the life of the box.
                self.scrapli_tn.close()
                self.running = True
                return
            if res == b"" and self.spins and self.spins % 30 == 0:
                # Belt and braces: a fresh prompt on a line of its own.
                self.wait_write("", wait=None)
            self.spins += 1
            return

        (ridx, match, res) = self.con_expect([b"ciscoasa>"], 1)
        if match:  # got a match!
            if ridx == 0:  # login
                if self.install_mode:
                    self.logger.debug("matched, ciscoasa>")
                    self.wait_write("", wait=None)
                    self.wait_write("", None)
                    self.wait_write("", wait="ciscoasa>")
                    self.running = True
                    return

                self.logger.debug("matched, ciscoasa>")
                self.wait_write("", wait=None)

                # run main config!
                self.apply_config()

                # startup time?
                startup_time = datetime.datetime.now() - self.start_time
                self.logger.debug("Startup complete in: %s" % startup_time)
                # mark as running
                self.running = True
                return

        # no match, if we saw some output from the router it's probably
        # booting, so let's give it some more time
        if res != b"":
            self.write_to_stdout(res)
            # reset spins if we saw some output
            self.spins = 0

        self.spins += 1

        return

    def gen_bootstrap_config(self):
        """Management address, the lab user and SSH: what vrnetlab needs."""
        v4_mgmt_address = vrnetlab.cidr_to_ddn(self.mgmt_address_ipv4)
        ipv6_address = self.render_optional_mgmt_config(
            "ipv6 address {address}", address=self.mgmt_address_ipv6
        )
        ipv6_route = self.render_optional_mgmt_config(
            "route management ::/0 {gateway} 1", gateway=self.mgmt_gw_ipv6
        )

        return f"""hostname {self.hostname}
aaa authentication ssh console LOCAL
aaa authentication enable console LOCAL
username {self.username} password {self.password} privilege 15
interface Management0/0
nameif management
security-level 100
ip address {v4_mgmt_address[0]} {v4_mgmt_address[1]}
{ipv6_address}
no shutdown
exit
route management 0.0.0.0 0.0.0.0 {self.mgmt_gw_ipv4} 1
{ipv6_route}
access-list MGMT_IN extended permit tcp any any eq ssh
access-group MGMT_IN in interface management
crypto key generate ecdsa elliptic-curve 256
ssh key-exchange group dh-group14-sha256
ssh 0.0.0.0 0.0.0.0 management
ssh ::/0 management
no ssh stricthostkeycheck
ssh timeout 60"""

    def apply_config(self):
        """Apply the full configuration"""
        self.logger.debug("Applying bootstrap configuration")

        scrapli_timeout = vrnetlab.getenv_uint(
            "SCRAPLI_TIMEOUT", vrnetlab.DEFAULT_SCRAPLI_TIMEOUT
        )

        def _open(conn):
            """Set the internal privilege level to 'exec' so scrapli knows what to do"""
            conn._current_priv_level = conn.privilege_levels["exec"]
            self.logger.debug(
                "Set initial privilege level to 'exec' to boostrap configuration"
            )

        asa_scrapli_dev = {
            "platform": "cisco_asa",
            "host": "127.0.0.1",
            "auth_bypass": True,
            "auth_strict_key": False,
            "auth_secondary": self.password,
            "timeout_socket": scrapli_timeout,
            "timeout_transport": scrapli_timeout,
            "timeout_ops": scrapli_timeout,
            "on_open": _open,
        }

        con = Scrapli(**asa_scrapli_dev)
        con.commandeer(conn=self.scrapli_tn)

        # On fresh ASA, typing 'enable' prompts to set up password
        self.logger.debug("Setting up initial enable password")
        result = con.send_interactive(
            interact_events=[
                ("enable", r"Password:", False),
                (self.password, r"Password:", True),
                # Send an empty character to force the prompt along
                (self.password, r"", False),
                ("", r"ciscoasa#", False),
            ],
            privilege_level="exec",
        )

        self.logger.debug("Entering configuration mode to handle reporting prompt")
        result = con.send_interactive(
            [
                (
                    "configure terminal",
                    r"Would you like to enable anonymous error reporting",
                    False,
                ),
                ("N", r"(config)#", False),
            ]
        )

        config_commands = self.gen_bootstrap_config()

        self.logger.debug("Sending configuration commands")
        con.send_configs(config_commands.splitlines())

        # Apply user-provided startup configuration if present
        if os.path.exists(STARTUP_CONFIG_FILE):
            self.logger.info("Startup configuration file found")
            with open(STARTUP_CONFIG_FILE, "r") as config:
                startup_config = config.read()
                self.logger.debug("Applying startup configuration")
                con.send_configs(startup_config.splitlines())
        else:
            self.logger.info("User provided startup configuration is not found.")

        self.logger.debug("Saving configuration")
        # Exit to privilege exec mode then save
        con.acquire_priv("privilege_exec")
        con.send_command("write memory")
        self.logger.debug("Closing connection")
        con.close()


class ASAv(vrnetlab.VR):
    def __init__(self, username, password, conn_mode, hostname):
        super(ASAv, self).__init__(username, password)
        self.vms = [ASAv_vm(username, password, conn_mode, hostname)]


class ASAv_installer(ASAv):
    """ASAv installer"""

    def __init__(self, username, password, conn_mode, hostname):
        super(ASAv_installer, self).__init__(username, password, conn_mode, hostname)
        self.vms = [ASAv_vm(username, password, conn_mode, hostname, install_mode=True)]

    def install(self):
        self.logger.info("Installing ASAv")
        asav = self.vms[0]
        while not asav.running:
            asav.work()
        asav.stop()
        self.logger.info("Installation complete")


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="")
    parser.add_argument(
        "--trace", action="store_true", help="enable trace level logging"
    )
    parser.add_argument("--hostname", default="asa", help="Hostname of the ASA VM")
    parser.add_argument("--username", default="admin", help="Username")
    parser.add_argument("--password", default="CiscoAsa1!", help="Password")
    parser.add_argument("--install", action="store_true", help="Install ASAv")
    parser.add_argument(
        "--connection-mode",
        default="vrxcon",
        help="Connection mode to use in the datapath",
    )
    args = parser.parse_args()

    LOG_FORMAT = "%(asctime)s: %(module)-10s %(levelname)-8s %(message)s"
    logging.basicConfig(format=LOG_FORMAT)
    logger = logging.getLogger()

    logger.setLevel(logging.DEBUG)
    if args.trace:
        logger.setLevel(1)

    if args.install:
        vr = ASAv_installer(
            args.username, args.password, args.connection_mode, args.hostname
        )
        vr.install()
    else:
        vr = ASAv(args.username, args.password, args.connection_mode, args.hostname)
        vr.start()
