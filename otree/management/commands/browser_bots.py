#!/usr/bin/env python
# -*- coding: utf-8 -*-

from __future__ import print_function
import sys
import time
import logging
import six
import requests
from django.core.management.base import BaseCommand
from six.moves import urllib
from django.core.urlresolvers import reverse
from django.conf import settings
import subprocess
from otree.room import ROOM_DICT
from otree.session import SESSION_CONFIGS_DICT, get_lcm
import websocket
import psutil

# how do i import this properly?
urljoin = urllib.parse.urljoin

FIREFOX_CMDS = {
    'windows': "C:/Program Files (x86)/Mozilla Firefox/firefox.exe",
    'mac': None,
    'linux': 'firefox'
}

CHROME_CMDS = {
    'windows': 'C:/Program Files (x86)/Google/Chrome/Application/chrome.exe',
    'mac': '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome',
    'linux': 'google-chrome',
}

if sys.platform.startswith("win"):
    platform = 'windows'
elif sys.platform.startswith("darwin"):
    platform = 'mac'
else:
    platform = 'linux'

CHROME_CMD = CHROME_CMDS[platform]

DEFAULT_ROOM_NAME = 'browser_bots'

ROOM_FLAG = '--room'
NUM_PARTICIPANTS_FLAG = '--num-participants'
SERVER_URL_FLAG = '--server-url'


RUNSERVER_WARNING = '''
You are using "otree runserver". In order to use browser bots,
you should run a multiprocess server (e.g. "otree webandworkers").
'''

SQLITE_WARNING = '''
WARNING: Your server is running are using SQLite.
Browser bots may not run properly.
We recommend using to Postgres or MySQL etc.
'''

AUTH_FAILURE_MESSAGE = """
Could not login to the server using your ADMIN_USERNAME
and ADMIN_PASSWORD from settings.py. If you are testing
browser bots on a remote server, make sure the username
and password on your local oTree installation match that
on the server.
"""

class Command(BaseCommand):
    help = "oTree: Run browser bots."

    def add_arguments(self, parser):
        parser.add_argument(
            'session_config_name', nargs='*',
            help='If omitted, all sessions in SESSION_CONFIGS are run'
        )
        parser.add_argument(
            SERVER_URL_FLAG, action='store', type=str, dest='server_url',
            default='http://127.0.0.1:8000',
            help="Server's root URL")
        ahelp = (
            'Numbers of participants. Examples: "12" or "4,12,18".'
            'Defaults to minimum for the session config.'
        )
        parser.add_argument(
            '-n', NUM_PARTICIPANTS_FLAG, type=str,
            help=ahelp)
        ahelp = (
            'Room to create the session in (see settings.ROOMS).'
            'Room must NOT have a participant_label_file.'
        )
        parser.add_argument(
            ROOM_FLAG, action='store', type=str,
            default=DEFAULT_ROOM_NAME,
            help=ahelp)

    def post(self, url, data=None):
        data = data or {}
        data.update({'csrfmiddlewaretoken': self.client.cookies['csrftoken']})
        return self.client.post(url, data)

    def handle(self, *args, **options):

        server_url = options['server_url']

        # if it doesn't start with http:// or https://,
        # assume http://
        if not server_url.startswith('http'):
            server_url = 'http://' + server_url
        room_name = options['room']

        num_participants = options['num_participants']
        if num_participants is not None:
            self.session_sizes = [int(n) for n in num_participants.split(',')]
        else:
            self.session_sizes = None
        self.wait_room_url = urljoin(
            server_url,
            reverse('assign_visitor_to_room', args=[room_name])
        )
        self.create_session_url = urljoin(
            server_url,
            reverse('create_browser_bots_session')
        )
        self.delete_session_url = urljoin(server_url, reverse('delete_sessions'))
        # TODO: use reverse? reverse('django.contrib.auth.views.login')
        login_url = urljoin(server_url, '/accounts/login/')

        # seems that urljoin doesn't work with ws:// urls
        # so do the ws replace after URLjoin
        websocket_url = urljoin(
            server_url,
            '/browser_bots_client/{}/'
        )
        self.websocket_url = websocket_url.replace(
            'http://', 'ws://').replace('https://', 'wss://')

        session_config_names = options["session_config_name"]
        if not session_config_names:
            # default to all session configs
            session_config_names = SESSION_CONFIGS_DICT.keys()

        self.browser_cmd = getattr(
            settings, 'BROWSER_COMMAND', CHROME_CMD
        )

        if 'chrome' in self.browser_cmd.lower():
            chrome_seen = False
            # FIXME: this is slow on Mac (maybe Linux too)
            # maybe use ps|grep instead
            for proc in psutil.process_iter():
                if 'chrome' in proc.name().lower():
                    chrome_seen = True
            if chrome_seen:
                print(
                    'WARNING: it looks like Chrome is already running. '
                    'You should quit Chrome before running this command.'
                )
            print(
                'For best results, use Chrome with no addons or ad-blocker. '
                'e.g. create a new Chrome profile.'
            )

        if room_name not in ROOM_DICT:
            raise ValueError(
                'No room named {} found in settings.ROOMS. '
                'You must either create a room named {}, '
                'or pass a {} argument with the name of your room. '
                'Note: room must NOT have a participant_label_file.'.format(
                    room_name,
                    room_name,
                    ROOM_FLAG,
                )
            )
        self.room_name = room_name

        self.client = requests.session()

        logging.getLogger("requests").setLevel(logging.WARNING)
        try:
            # populate CSRF cookie
            resp = self.client.get(login_url)
        except:
            raise Exception(
                'Could not connect to server at {}.'
                'Before running this command, '
                'you need to run the server (see {} flag).'.format(
                    server_url,
                    SERVER_URL_FLAG
                )
            )
        if not resp.ok:
            raise Exception(
                'Could not open page at {}.'
                '(HTTP status code: {})'.format(
                    server_url,
                    resp.status_code,
                )
            )

        # login
        resp = self.post(
            login_url,
            data={
                'username': settings.ADMIN_USERNAME,
                'password': settings.ADMIN_PASSWORD,
            },
        )
        if '/accounts/login' in resp.url:
            raise Exception(AUTH_FAILURE_MESSAGE)

        # .get just returns server readiness info
        resp = self.client.get(self.create_session_url)
        server_check = resp.json()

        if server_check['runserver']:
            print(RUNSERVER_WARNING)
        if server_check['sqlite']:
            print(SQLITE_WARNING)

        self.max_name_length = max(
            len(config_name) for config_name in session_config_names
        )

        self.total_time_spent = 0
        self.session_codes = []
        try:
            for session_config_name in session_config_names:
                session_config = SESSION_CONFIGS_DICT[session_config_name]

                if self.session_sizes is None:
                    session_sizes = [session_config['num_demo_participants']]
                else:
                    session_sizes = self.session_sizes

                for num_participants in session_sizes:
                    self.run_session_config(
                        session_config_name,
                        num_participants
                    )
        finally:
            # delete sessions after all sessions have run.
            # I do it here, rather than after each session, for a few reasons:
            # - Can do it in one request rather than many
            # - Minimize issues caused by deleting the session immediately
            #   after it finishes
            # - If performance slows after only a few sessions,
            #   that's something we should know about!
            if self.session_codes:
                resp = self.post(
                    self.delete_session_url,
                    data={'item-action': self.session_codes}
                )
                assert resp.ok, 'Failed to delete session.'

        print('Total: {} seconds'.format(
            round(self.total_time_spent, 1)
        ))

    def run_session_config(self, session_config_name, num_participants):
        args = [self.browser_cmd]
        for i in range(num_participants):
            args.append(self.wait_room_url)

        try:
            browser_process = subprocess.Popen(args)
        except Exception as exception:
            msg = (
                'Could not launch browser. '
                'Check your settings.BROWSER_COMMAND. {}'
            )

            six.reraise(
                type(exception),
                type(exception)(msg.format(exception)),
                sys.exc_info()[2])

        row_fmt = "{:<%d} {:>2} participants..." % (self.max_name_length + 1)
        print(row_fmt.format(session_config_name, num_participants), end='')

        resp = self.post(
            self.create_session_url,
            data={
                'session_config_name': session_config_name,
                'num_participants': num_participants,
            }
        )

        assert resp.ok, 'Failed to create session'
        session_code = resp.content.decode('utf-8')
        self.session_codes.append(session_code)

        websocket_url = self.websocket_url.format(session_code)
        ws = websocket.create_connection(
            websocket_url
        )

        bot_start_time = time.time()

        seen_participant_codes = set()
        participants_finished = 0

        while participants_finished < num_participants:
            code = ws.recv()
            # guard against the same participant sending the message
            # twice somehow
            if code not in seen_participant_codes:
                seen_participant_codes.add(code)
                participants_finished += 1

        time_spent = round(time.time() - bot_start_time, 1)
        print('...finished in {} seconds'.format(time_spent))
        self.total_time_spent += time_spent

        # TODO:
        # - if Chrome/FF is already running when the browser is launched,
        # this does nothing.
        # also, they report a crash (in Firefox it blocks the app from
        # starting again), in Chrome it's just a side notice
        browser_process.terminate()
