#!/usr/bin/python3


# pylint: disable=line-too-long


"""
Consume logs sent by syslog-ng into AMQ server and push them to local syslog-ng UDP endpoint (spoofing source address with original IP)
"""


import time
import logging
import datetime
from typing import List, Optional

import pika  # type: ignore
from scapy.packet import Raw  # type: ignore
from scapy.layers.inet import IP, UDP  # type: ignore
from scapy.all import conf  # type: ignore

PIKA_VERSION = tuple(int(x) for x in pika.__version__.split("."))

# 0.4.0 has ValidatorError used in middleware
assert PIKA_VERSION >= (1, 0, 1), "This application needs pika >= 1.0.1"


# disable scapy promiscuous mode
conf.sniff_promisc = 0


class AmqLogsToUdp:  # pylint: disable=too-many-instance-attributes
    """
    Consume logs sent by syslog-ng into AMQ server and push them to local syslog-ng UDP endpoint (spoofing source address with original IP)

    :param amq_host: Hostname or IP address of AMQ server to use
    :type amq_host: str
    :param amq_queue: Name of the AMQ queue to consume logs messages from
    :type amq_queue: str
    :param amq_port: AMQ server port
    :type amq_port: int, defaults to 5672
    :param amq_vhost: AMQ virtual host to use
    :type amq_vhost: str, defaults to /
    :param amq_username: Username to authenticate with AMQ server
    :type amq_username: str, defaults to guest
    :param amq_password: Password to authenticate with AMQ server
    :type amq_password: str, defaults to guest
    :param amq_prefetch: Number of AMQ messages to get per batch
    :type amq_prefetch: str, defaults to 1000
    :param amq_routing_key_rfc3164_bsd: Routing key used by messages using RFC3164 BSD legacy format
    :type amq_routing_key_rfc3164_bsd: str, defaults to rfc3164-bsd
    :param amq_routing_key_rfc5424_ietf: Routing key used by messages using RFC5424 IETF new format
    :type amq_routing_key_rfc5424_ietf: str, defaults to rfc5424-ietf
    :param udp_host: Destination Syslog UDP host
    :type udp_host: str, defaults to 127.0.0.1
    :param udp_port_rfc3164_bsd: Destination Syslog UDP port, for messages using RFC3164 BSD legacy format
    :type udp_port_rfc3164_bsd: int, defaults to 514
    :param udp_port_rfc5424_ietf: Destination Syslog UDP port, for messages using RFC5424 IETF new format, we still use UDP to spoof source address
    :type udp_port_rfc5424_ietf: int, defaults to 601
    :param exclude_patterns: Exclude message matching one of this pattern
    :type exclude_patterns: list, optional
    """

    def __init__(  # pylint: disable=too-many-arguments
        self,
        amq_host: str,
        amq_queue: str,
        amq_port: int = 5672,
        amq_vhost: str = "/",
        amq_username: str = "guest",
        amq_password: str = "guest",
        amq_prefetch: int = 1000,
        amq_routing_key_rfc3164_bsd: str = "rfc3164-bsd",
        amq_routing_key_rfc5424_ietf: str = "rfc5424-ietf",
        udp_host: str = "127.0.0.1",
        udp_port_rfc3164_bsd: int = 514,
        udp_port_rfc5424_ietf: int = 601,
        exclude_patterns: Optional[List] = None,
    ) -> None:
        assert isinstance(amq_host, str) and amq_host, "amq_host parameter must be a non-empty string"
        assert isinstance(amq_queue, str) and amq_queue, "amq_queue parameter must be a non-empty string"
        assert isinstance(amq_port, int) and 1 <= amq_port <= 65535, "amq_port parameter must be an integer between 1 and 65535"
        assert isinstance(amq_vhost, str) and amq_vhost, "amq_vhost parameter must be a non-empty string"
        assert isinstance(amq_username, str) and amq_username, "amq_username parameter must be a non-empty string"
        assert isinstance(amq_password, str) and amq_password, "amq_password parameter must be a non-empty string"
        assert isinstance(amq_prefetch, int) and amq_prefetch > 0, "amq_prefetch parameter must be a positive integer"
        assert isinstance(amq_routing_key_rfc3164_bsd, str), "amq_routing_key_rfc3164_bsd parameter must be a string"
        assert isinstance(amq_routing_key_rfc5424_ietf, str), "amq_routing_key_rfc5424_ietf parameter must be a string"
        assert isinstance(udp_host, str) and udp_host, "udp_host parameter must be a non-empty string"
        assert isinstance(udp_port_rfc3164_bsd, int) and 1 <= udp_port_rfc3164_bsd <= 65535, "udp_port_rfc3164_bsd parameter must be an integer between 1 and 65535"
        assert isinstance(udp_port_rfc5424_ietf, int) and 1 <= udp_port_rfc5424_ietf <= 65535, "udp_port_rfc5424_ietf parameter must be an integer between 1 and 65535"
        assert (
            exclude_patterns is None or isinstance(exclude_patterns, list) and all(isinstance(x, str) and x for x in exclude_patterns)
        ), "exclude_patterns parameter must be None or a list of non-empty strings"
        self.amq_host = amq_host
        self.amq_queue = amq_queue
        self.amq_port = amq_port
        self.amq_vhost = amq_vhost
        self.amq_username = amq_username
        self.amq_password = amq_password
        self.amq_prefetch = amq_prefetch
        self.amq_routing_key_rfc3164_bsd = amq_routing_key_rfc3164_bsd
        self.amq_routing_key_rfc5424_ietf = amq_routing_key_rfc5424_ietf
        self.udp_host = udp_host
        self.udp_port_rfc3164_bsd = udp_port_rfc3164_bsd
        self.udp_port_rfc5424_ietf = udp_port_rfc5424_ietf
        if exclude_patterns is None:
            exclude_patterns = []
        self.exclude_patterns = exclude_patterns
        self.exclude_patterns_bytes = [bytes(x, "utf-8") for x in self.exclude_patterns]

        self.logger = logging.getLogger(self.__class__.__name__)
        # For periodic stats logger
        self.consume_log_count_rfc3164_bsd = 0
        self.consume_log_count_rfc5424_ietf = 0
        self.consume_log_count_unspecified = 0
        self.consume_log_count_unknown = 0
        self.consume_log_dt = self.utc_now

        self.socket = conf.L3socket()

    @property
    def utc_now(self) -> datetime.datetime:
        """
        Return UTC now

        :getter: Now typed with UTC timezone
        """

        return datetime.datetime.now(tz=datetime.timezone.utc)

    @property
    def conn_params(self) -> pika.ConnectionParameters:
        """
        Return pika ConnectionParameters object

        :getter: ConnectionParameters object with all acccess parameters and credentials
        """

        credentials = pika.PlainCredentials(self.amq_username, self.amq_password)
        parameters = pika.ConnectionParameters(host=self.amq_host, port=self.amq_port, virtual_host=self.amq_vhost, credentials=credentials, heartbeat=60)
        return parameters

    def udp_publish(self, *, body: bytes, source_ip: str, destination_port: int) -> None:
        """
        Publish given syslog message as raw bytes to destination UDP endpoint and spoof source address with given source_ip

        :param body: Syslog message as bytes
        :type body: str
        :param source_ip: Spoof source IP using this address
        :type source_ip: str
        :param destination_port: Destination port because we may use different ports for BSD and IETF messages
        :type destination_port: int
        """

        packet = IP(src=source_ip, dst=self.udp_host) / UDP(dport=destination_port) / Raw(load=body)
        self.socket.send(packet)

    def on_message(
        self,
        channel: pika.adapters.blocking_connection.BlockingChannel,
        method_frame: pika.spec.Basic.Deliver,
        header_frame: pika.spec.BasicProperties,
        body: bytes,
    ) -> None:
        """
        Callback receiving message consumed from AMQ server

        :param channel: Pika object representing channel connected to AMQ server
        :type channel: pika.adapters.blocking_connection.BlockingChannel
        :param method_frame: Pika object representing low level protocol properties
        :type method_frame: pika.spec.Basic.Deliver
        :param header_frame: Pika object representing headers metadata
        :type header_frame: pika.spec.BasicProperties
        :param body: Raw message content
        :type body: bytes
        """

        # Extract routing key to see what type of message is being received
        routing_key = method_frame.routing_key
        if routing_key == self.amq_routing_key_rfc3164_bsd:
            destination_port = self.udp_port_rfc3164_bsd
            self.consume_log_count_rfc3164_bsd += 1
        elif routing_key == "":  # For compat purpose
            destination_port = self.udp_port_rfc3164_bsd
            self.consume_log_count_unspecified += 1
        elif routing_key == self.amq_routing_key_rfc5424_ietf:
            destination_port = self.udp_port_rfc5424_ietf
            self.consume_log_count_rfc5424_ietf += 1
        else:
            self.consume_log_count_unknown += 1
            channel.basic_ack(delivery_tag=method_frame.delivery_tag)
            return

        source_ip = header_frame.headers["SOURCEIP"]
        match_exclude = any(x in body for x in self.exclude_patterns_bytes)
        if not match_exclude:
            self.udp_publish(body=body, source_ip=source_ip, destination_port=destination_port)

        # Just for logging every 60 seconds
        # Of course if there's nothing to consume there will be no log at all
        # I thought about writing this using asyncio but too much work for no
        # real improvments
        if self.utc_now - datetime.timedelta(seconds=60) > self.consume_log_dt:
            self.logger.info(
                "Consumed %d RFC3164-BSD, %d RFC5424-IETF, %d untagged as RFC3164-BSD and %d unknown as discard messages for the last 60s",
                self.consume_log_count_rfc3164_bsd,
                self.consume_log_count_rfc5424_ietf,
                self.consume_log_count_unspecified,
                self.consume_log_count_unknown,
            )
            self.logger.info("Last log entry was: %s from %s", body, source_ip)
            self.consume_log_dt = self.utc_now
            self.consume_log_count = 0

        channel.basic_ack(delivery_tag=method_frame.delivery_tag)

    def consume_forever(self) -> None:
        """
        Start endless loop consuming AMQ messages from server
        """

        while True:

            try:
                self.logger.info(
                    "Connecting to AMQ server at %s:%d using user %s and queue %s", self.amq_host, self.amq_port, self.amq_username, self.amq_queue
                )
                connection = pika.BlockingConnection(self.conn_params)
                channel = connection.channel()
                channel.basic_qos(prefetch_count=self.amq_prefetch)
                channel.basic_consume(self.amq_queue, self.on_message)
                self.logger.info("Connected and ready to consume messages with prefetch_count=%d", self.amq_prefetch)
                try:
                    channel.start_consuming()
                except KeyboardInterrupt:
                    channel.stop_consuming()
                    connection.close()
                    break
            except pika.exceptions.ConnectionClosedByBroker:
                self.logger.warning("Remote server %s:%d closed connection properly, retrying in 60s", self.amq_host, self.amq_port)
                time.sleep(60)
                continue
            except pika.exceptions.AMQPChannelError as exc:
                self.logger.exception("Got exception on channel, crashing myself to trigger restart in 60s: %s: %s", exc.__class__.__name__, exc)
                time.sleep(60)
                raise
            except pika.exceptions.AMQPConnectionError as exc:
                self.logger.warning(
                    "Remote server %s:%d closed connection incorrectly, retrying in 60s: %s: %s", self.amq_host, self.amq_port, exc.__class__.__name__, exc
                )
                time.sleep(60)
                continue
            except Exception as exc:  # pylint: disable=broad-except
                self.logger.exception("Unhandled exception occurred, crashing myself to trigger restart in 60s: %s: %s", exc.__class__.__name__, exc)
                time.sleep(60)
                raise


if __name__ == "__main__":

    import os
    import sys
    import shutil
    import argparse

    def cli_arguments() -> argparse.Namespace:
        """
        Parse argument from command line and return Namespace object

        :return: Namespace object containing all properties (dash replaced by underscore)
        :rtype: argparse.Namespace
        """

        os.environ["COLUMNS"] = str(shutil.get_terminal_size().columns)

        parser = argparse.ArgumentParser(description=__doc__.strip(), formatter_class=argparse.ArgumentDefaultsHelpFormatter)

        group_amq = parser.add_argument_group("AMQ", "AMQ (RabbitMQ) server parameters")
        group_amq.add_argument("--amq-host", type=str, required=True, help="Hostname or IP address of AMQ server to use", metavar="10.1.0.1")
        group_amq.add_argument("--amq-port", type=int, required=True, help="AMQ server port", metavar="5672")
        group_amq.add_argument("--amq-vhost", type=str, required=True, help="AMQ virtual host to use", metavar="/")
        group_amq.add_argument("--amq-username", type=str, required=True, help="Username to authenticate with AMQ server", metavar="guest")
        group_amq.add_argument("--amq-password", type=str, required=True, help="Password to authenticate with AMQ server", metavar="guest")
        group_amq.add_argument("--amq-queue", type=str, required=True, help="Name of the AMQ queue to consume logs messages from", metavar="syslog-ng-for-peer")
        group_amq.add_argument("--amq-prefetch", type=int, required=True, help="Number of AMQ messages to get per batch", metavar="1000")
        group_amq.add_argument("--amq-routing-key-rfc3164-bsd", type=str, required=True, help="Routing key used by messages using RFC3164 BSD legacy format", metavar="rfc3164-bsd")
        group_amq.add_argument("--amq-routing-key-rfc5424-ietf", type=str, required=True, help="Routing key used by messages using RFC5424 IETF new format", metavar="rfc5424-ietf")

        group_udp = parser.add_argument_group("Syslog-NG", "UDP destination for Syslog-NG")
        group_udp.add_argument(
            "--udp-host",
            type=str,
            required=True,
            help="Destination Syslog UDP host (Do not use 127.0.0.1 here, message gets corrupted and I don't know why",
            metavar="10.1.0.2",
        )
        group_udp.add_argument(
            "--udp-port-rfc3164-bsd",
            type=int,
            required=True,
            help="Destination Syslog UDP port for messages using RFC3164 BSD legacy format (You probably want to use a different port, otherwise messages will loop between peers)",
            metavar="515",
        )
        group_udp.add_argument(
            "--udp-port-rfc5424-ietf",
            type=int,
            required=True,
            help="Destination Syslog UDP port for messages using RFC5424 IETF new format (You probably want to use a different port, otherwise messages will loop between peers)",
            metavar="602",
        )

        message = parser.add_argument_group("Messages", "Options related to messages payload")
        message.add_argument(
            "--exclude-patterns",
            type=str,
            nargs="*",
            required=False,
            help="Exclude messages matching this pattern",
            metavar="dsd17-periodicmeasures-q2db pattern2",
        )

        parsed = parser.parse_args()
        return parsed

    def main() -> None:
        """
        Start application
        """

        if os.getenv("NO_LOGS_TS", None) is not None:
            log_formatter = "%(levelname)-8s [%(name)s] %(message)s"
        else:
            log_formatter = "%(asctime)s %(levelname)-8s [%(name)s] %(message)s"
        logging.basicConfig(level=logging.INFO, format=log_formatter, stream=sys.stdout)
        logging.getLogger("pika").setLevel(logging.WARNING)
        logging.info("Application is starting in 5 seconds")

        try:
            from setproctitle import setproctitle

            setproctitle(" ".join(sys.argv))
        except Exception as exc:  # pylint: disable=broad-except
            logging.warning("Unable to set process name: %s: %s", exc.__class__.__name__, exc)

        config = cli_arguments()

        consumer = AmqLogsToUdp(
            amq_host=config.amq_host,
            amq_queue=config.amq_queue,
            amq_port=config.amq_port,
            amq_vhost=config.amq_vhost,
            amq_username=config.amq_username,
            amq_password=config.amq_password,
            amq_prefetch=config.amq_prefetch,
            amq_routing_key_rfc3164_bsd=config.amq_routing_key_rfc3164_bsd,
            amq_routing_key_rfc5424_ietf=config.amq_routing_key_rfc5424_ietf,
            udp_host=config.udp_host,
            udp_port_rfc3164_bsd=config.udp_port_rfc3164_bsd,
            udp_port_rfc5424_ietf=config.udp_port_rfc5424_ietf,
            exclude_patterns=config.exclude_patterns,
        )
        # In case syslog-ng is not fully ready yet, as a safety measure
        time.sleep(5)
        consumer.consume_forever()

    try:
        main()
    except KeyboardInterrupt:
        logging.info("Exiting on SIGINT")
