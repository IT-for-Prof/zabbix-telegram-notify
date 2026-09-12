# -*- coding: utf-8 -*-
########################
#    Sokolov Dmitry    #
# xx.sokolov@gmail.com #
#  https://t.me/ZbxNTg #
########################
__author__ = "Sokolov Dmitry"
__maintainer__ = "Sokolov Dmitry"
__license__ = "MIT"
import argparse
from argparse import RawTextHelpFormatter


class ArgParsing:
    """The three positional arguments are the Zabbix media type parameters,
    in order: {ALERT.SENDTO}, {ALERT.SUBJECT}, {ALERT.MESSAGE}."""

    def __init__(self):
        self.parser = None

    def create_parser(self):
        self.parser = argparse.ArgumentParser(
            prog='znt',
            description='Zabbix notifications to Telegram',
            epilog='(c) Dmitry Sokolov 2019 @ https://github.com/xxsokolov/',
            add_help=False, formatter_class=RawTextHelpFormatter)

        self.parser.add_argument('username', nargs='?', help='Set username Telegram')
        self.parser.add_argument('subject', nargs='?', help='Set subject')
        self.parser.add_argument('messages', nargs='?', help='Set message')
        self.parser.add_argument('token', nargs='?', help='Set token', default=False)
        self.parser.add_argument('--zabbix-pass', dest='zabbix_pass', default=None,
                                 help='Zabbix password for fetching charts. Lets a '
                                      'media type pass it as a secret macro instead '
                                      'of keeping it in a file on the host.')
        self.parser.add_argument('--quiet-hours', dest='quiet_hours', default=None,
                                 help='Quiet window in Zabbix time period syntax. '
                                      'Overrides quiet_hours from the .cfg, so a '
                                      'media type can carry its own policy.')
        self.parser.add_argument('--dry-run', dest='dry_run', action='store_true',
                                 help='Build the message and report what would be '
                                      'sent, without calling Telegram.')
        self.parser.add_argument('--debug', type=str, nargs='?', const=True,
                                 default=False, help='Debug mode')

        return self.parser
