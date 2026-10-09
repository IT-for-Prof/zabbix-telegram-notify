#!/usr/bin/python3
# -*- coding: utf-8 -*-
"""Regression tests for the defects the 2026-09-09 audit found.

No framework, no fixtures: python3 tests/test_parse.py. mon runs 3.6.8, so
nothing here may use syntax newer than that.

zbxTelegram.py talks to Telegram, Zabbix and Pillow at import time. Those are
stubbed out below; xmltodict is real, because parsing the envelope is exactly
what is under test.
"""
import io
import os
import re
import stat
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)


class _StubError(Exception):
    """Stands in for telebot.apihelper.ApiException.

    It has to be a real exception class: `except apihelper.ApiException` against
    a plain stub raises "catching classes that do not inherit from BaseException",
    which would make every error branch in send_messages unreachable from here.
    """


class _Stub(object):
    """Any attribute, any call, no network."""

    def __getattr__(self, name):
        if name.endswith(('Exception', 'Error')):
            return _StubError
        return _Stub()

    def __call__(self, *args, **kwargs):
        return _Stub()


for _name in ('telebot', 'telebot.apihelper', 'telebot.types',
              'requests', 'urllib3', 'urllib3.exceptions', 'PIL'):
    sys.modules[_name] = _Stub()

_TMP = tempfile.mkdtemp(prefix='znt-test-')


def _write_test_config():
    """The shipped example, with the paths pointed somewhere writable."""
    src = io.open(os.path.join(ROOT, 'zbxTelegram.cfg.example'), encoding='utf-8').read()
    # By key name, not by literal path: the example's default has moved once
    # already, which silently disarmed this substitution.
    src = re.sub(r'(?m)^config_cache_file\s*=.*',
                 'config_cache_file = ' + os.path.join(_TMP, 'id.cache'), src)
    src = re.sub(r'(?m)^config_log_file\s*=.*',
                 'config_log_file = ' + os.path.join(_TMP, 'znt.log'), src)
    path = os.path.join(_TMP, 'zbxTelegram.cfg')
    io.open(path, 'w', encoding='utf-8').write(src)
    return path


os.environ['ZNT_CONFIG'] = _write_test_config()
os.environ['ZNT_TG_TOKEN'] = '123456:TEST-TOKEN-NOT-A-REAL-ONE-000000000000'
os.environ['ZNT_ZABBIX_PASS'] = 'test'
sys.argv = ['zbxTelegram.py', 'testuser', '{Problem}: test', '<root/>']

import zbxTelegram as znt  # noqa: E402  (import order is deliberate: stubs first)


ENVELOPE = u'''<?xml version="1.0" encoding="UTF-8"?>
<root><body><messages>{message}</messages></body>
<settings><graphs>False</graphs><graphlinks>False</graphlinks>
<triggerlinks>False</triggerlinks><hostlinks>False</hostlinks>
<acklinks>False</acklinks><eventlinks>False</eventlinks>
<eventtag>False</eventtag><eventidtag>False</eventidtag>
<itemidtag>False</itemidtag><triggeridtag>False</triggeridtag>
<actionidtag>False</actionidtag><hostidtag>False</hostidtag>
<zntsettingstag>False</zntsettingstag><zntmentions>False</zntmentions>
<keyboard>False</keyboard><graphs_period>default</graphs_period>
<host>testhost</host><itemid>60605</itemid><triggerid>39303</triggerid>
<eventid>130144443</eventid><actionid>22</actionid><hostid>10084</hostid>
<title>test</title><triggerurl></triggerurl><eventtags></eventtags>
</settings></root>'''


# --- config -----------------------------------------------------------------

def test_config_loads_from_cfg():
    """The shipped example parses, types are coerced, secrets come from env."""
    assert znt.body_messages_max_symbol == 600, znt.body_messages_max_symbol
    assert znt.config_debug_mode is False, znt.config_debug_mode
    assert znt.zabbix_status_emoji_map['Not classified'] == u'⁉️'
    # a quoted value keeps the whitespace configparser would otherwise strip
    assert znt.body_messages_tags_delimiter == ' ', repr(znt.body_messages_tags_delimiter)
    # never from the file
    assert znt.tg_token == os.environ['ZNT_TG_TOKEN']


def test_secrets_may_come_from_the_cfg_as_a_last_resort():
    """The README offers the .cfg as the escape from `ps` and database exposure
    for anyone on a shared host. It has to actually work: load_config used to
    overwrite both keys from the environment unconditionally, so a token written
    to the file was discarded and the script exited on "token is not set"."""
    src = io.open(os.path.join(ROOT, 'zbxTelegram.cfg.example'), encoding='utf-8').read()
    src = src.replace('[telegram]', '[telegram]\ntg_token = FROM-THE-CFG\n'
                                    'zabbix_api_pass = PASS-FROM-THE-CFG')
    # By key name, not by literal path: the example's default has moved once
    # already, which silently disarmed this substitution.
    src = re.sub(r'(?m)^config_cache_file\s*=.*',
                 'config_cache_file = ' + os.path.join(_TMP, 'id.cache'), src)
    src = re.sub(r'(?m)^config_log_file\s*=.*',
                 'config_log_file = ' + os.path.join(_TMP, 'znt.log'), src)
    path = os.path.join(_TMP, 'with-secrets.cfg')
    io.open(path, 'w', encoding='utf-8').write(src)

    saved = dict(os.environ)
    try:
        os.environ.pop('ZNT_TG_TOKEN', None)
        os.environ.pop('ZNT_ZABBIX_PASS', None)
        cfg = znt.load_config(path)
        assert cfg['tg_token'] == 'FROM-THE-CFG', cfg['tg_token']
        assert cfg['zabbix_api_pass'] == 'PASS-FROM-THE-CFG', cfg['zabbix_api_pass']
        # and the environment still wins over the file
        os.environ['ZNT_TG_TOKEN'] = 'FROM-THE-ENVIRONMENT'
        assert znt.load_config(path)['tg_token'] == 'FROM-THE-ENVIRONMENT'
    finally:
        os.environ.clear()
        os.environ.update(saved)


def test_config_has_no_secrets():
    """A public repo must not ship a token or a password in the example."""
    src = io.open(os.path.join(ROOT, 'zbxTelegram.cfg.example'), encoding='utf-8').read()
    for forbidden in ('tg_token =', 'zabbix_api_pass =', 'tg_proxy_server ='):
        assert forbidden not in src, forbidden


def test_chat_id_cache_is_not_world_readable():
    znt.get_cache('nobody')
    os.chmod(znt.config_cache_file, 0o666)
    znt.get_cache('nobody')
    assert stat.S_IMODE(os.stat(znt.config_cache_file).st_mode) == 0o640


# --- defect 1: escalation-canceled banner -----------------------------------

def test_banner_before_xml_is_kept_and_stripped():
    banner = 'NOTE: Escalation canceled: trigger disabled.\nLast message sent:'
    parsed = znt.xml_parsing(banner + '\n' + ENVELOPE.format(message='body text'))
    assert parsed is not None, 'envelope with a banner must still parse'
    assert parsed['message'].startswith('NOTE: Escalation canceled'), parsed['message']
    assert 'body text' in parsed['message'], parsed['message']


# --- defect 2: a message that is not an XML envelope ------------------------

def test_unparseable_message_is_not_fatal():
    """Used to be KeyError 'root' -> exit(1) with nothing sent."""
    assert znt.xml_parsing('Zabbix database is not available.') is None
    assert znt.xml_parsing('') is None
    assert znt.xml_parsing(None) is None


def test_plain_text_alert_is_delivered_instead_of_exiting():
    """The internal "Zabbix database is not available" alert is plain text.
    It fired three times in 60 days and was silently dropped all three."""
    sent = {}

    def fake_send(sent_to, message, graphs_png, *args, **kwargs):
        sent['to'] = sent_to
        sent['message'] = message
        sent['graphs_png'] = graphs_png
        sent['disable_notification'] = kwargs.get('disable_notification')

    real_send, real_args = znt.send_messages, znt.args
    znt.send_messages = fake_send
    znt.args = znt.ArgParsing().create_parser().parse_args(
        ['zabbixadmin', 'Zabbix database is not available.',
         'Zabbix database is not available.'])
    try:
        znt.main()
    except SystemExit as err:
        assert err.code == 0, 'plain text delivery must not exit non-zero'
    finally:
        znt.send_messages, znt.args = real_send, real_args

    assert sent, 'nothing was sent at all'
    assert 'Zabbix database is not available.' in sent['message'], sent['message']
    assert sent['graphs_png'] is False, 'no chart without an envelope'
    # No status literal in an internal alert, so it must arrive loud.
    assert sent['disable_notification'] is False, sent['disable_notification']


def test_unexpanded_recipient_is_reported_as_such():
    """Zabbix hands us {ALERT.SENDTO} verbatim when the user media "Send to"
    field is unset. Nothing can deliver that, but the log must say why rather
    than blaming the bot or the cache file."""
    import logging

    class Grab(logging.Handler):
        def __init__(self):
            logging.Handler.__init__(self)
            self.lines = []

        def emit(self, record):
            self.lines.append(record.getMessage())

    grab = Grab()
    logging.getLogger().addHandler(grab)
    try:
        znt.get_send_id('{ALERT.SENDTO}')
    except SystemExit:
        pass
    finally:
        logging.getLogger().removeHandler(grab)
    text = ' '.join(grab.lines)
    assert 'unexpanded' in text.lower(), text
    assert '{ALERT.SENDTO}' in text, text


def test_caption_length_is_measured_in_utf16_units_like_telegram():
    """Telegram counts UTF-16 code units, so every non-BMP severity emoji is 2.

    Counting python characters undercounts, and since the trimmer stops exactly
    at the limit, an emoji-bearing caption lands just past it and comes back
    "caption is too long" -- defect 3 returning by the back door.
    """
    assert znt.visible_length(u'\U0001F6A8') == 2, 'astral emoji is 2 units'
    assert znt.visible_length(u'\U0001F6A8 \U0001F494') == 5
    assert znt.visible_length('abc') == 3

    subject = u'\U0001F6A8 Disaster \U0001F494 ' + 'S' * 450
    links = ' '.join(u'<a href="https://zbx.example/i/{}">\U0001F4CA</a>'.format(n)
                     for n in range(40))
    message = znt.render_message(subject, 'B' * 2000, links, '', MORE_URL, 1024)
    utf16_units = len(znt.visible_text(message).encode('utf-16-le')) // 2
    assert utf16_units <= 1024, utf16_units


def test_broken_envelope_is_never_delivered_as_raw_xml():
    """A document that parses but is missing fields is our own action XML being
    wrong, not a plain-text alert. Delivering the raw XML and exiting 0 would
    report success for something nobody can read."""
    broken = ENVELOPE.format(message='real alert text').replace(
        '<title>test</title>', '')
    try:
        znt.xml_parsing(broken)
    except SystemExit as err:
        assert err.code == 1, err.code
    else:
        raise AssertionError('a malformed envelope must not be reported as success')


def test_empty_message_body_does_not_crash():
    """xmltodict maps <messages></messages> to None."""
    parsed = znt.xml_parsing(ENVELOPE.format(message=''))
    assert parsed is not None and parsed['message'] == '', parsed
    message = znt.render_message('s', parsed['message'], '', '', MORE_URL, 4096)
    assert '<b>s</b>' in message, message


class _Tripwire(object):
    """Any call is a failure: used to prove --dry-run never reaches Telegram."""

    def __init__(self):
        self.calls = []

    def __getattr__(self, name):
        def fail(*args, **kwargs):
            self.calls.append(name)
            raise AssertionError('dry run called Telegram: ' + name)
        return fail


def test_dry_run_reports_without_sending():
    """--dry-run runs the whole pipeline and stops at the Telegram call, so a
    deployment can be rehearsed against a real envelope without a real message."""
    tripwire = _Tripwire()
    buffer = io.StringIO()
    real_bot, real_args, real_stdout = znt.bot, znt.args, sys.stdout
    znt.bot = tripwire
    znt.args = znt.ArgParsing().create_parser().parse_args(
        ['--dry-run', 'testuser', '{Resolved}: quiet one',
         ENVELOPE.format(message='the body text')])
    sys.stdout = buffer
    try:
        znt.main()
    except SystemExit as err:
        code = err.code
    finally:
        znt.bot, znt.args, sys.stdout = real_bot, real_args, real_stdout

    assert code == 0, code
    assert tripwire.calls == [], tripwire.calls
    printed = buffer.getvalue()
    assert 'the body text' in printed, printed
    assert u'\u2705' in printed, 'the resolved emoji should be substituted'


def test_dry_run_returns_so_the_caller_keeps_its_exit_code():
    """send_messages must return under --dry-run, not exit.

    The built-in test-message path reports whether the Zabbix connection works
    through its own exit code, and exit(0) inside send_messages made that
    unreachable -- a rehearsal claimed success with the backend unreachable.
    """
    real_args = znt.args
    znt.args = znt.ArgParsing().create_parser().parse_args(
        ['--dry-run', 'testuser', '{Problem}: x', '<root/>'])
    buffer = io.StringIO()
    real_stdout, sys.stdout = sys.stdout, buffer
    try:
        result = znt.send_messages('testuser', '<b>msg</b>', False)
    finally:
        sys.stdout = real_stdout
        znt.args = real_args
    assert result is None, result
    assert 'msg' in buffer.getvalue(), buffer.getvalue()


def test_telegram_retry_configuration():
    """Telegram transport is configured for bounded transient-error retries."""
    assert znt.apihelper.RETRY_ON_ERROR is True
    assert znt.apihelper.RETRY_ENGINE == 1
    assert znt.apihelper.MAX_RETRIES == 3
    assert znt.apihelper.RETRY_TIMEOUT == 1


def _flood_error(retry_after):
    err = _StubError('Error code: 429. Description: Too Many Requests: '
                     'retry after {}'.format(retry_after))
    err.error_code = 429
    err.result_json = {'ok': False, 'error_code': 429,
                       'parameters': {'retry_after': retry_after}}
    return err


class _FloodedBot(object):
    """send_message answers 429 a given number of times, then succeeds."""

    def __init__(self, floods, retry_after):
        self.floods, self.retry_after, self.sent = floods, retry_after, 0

    def send_message(self, **kwargs):
        if self.floods:
            self.floods -= 1
            raise _flood_error(self.retry_after)
        self.sent += 1

    def get_me(self):
        return type('Me', (), {'username': 'testbot', 'id': 1})()


def test_flood_limit_is_waited_out_instead_of_losing_the_alert():
    """A DNS outage escalated ~50 problems to one group in two minutes.
    Telegram allows ~20 messages a minute per group and answered 429 with
    retry_after; the script exited 1 at once, Zabbix's three attempts 30 s apart
    landed inside the same flood window, and an alert was lost."""
    slept = []
    bot = _FloodedBot(floods=2, retry_after=3)
    saved = znt.bot, znt.get_send_id, znt.time.sleep, znt.args
    znt.bot, znt.get_send_id, znt.time.sleep = bot, lambda to: 1, slept.append
    znt.args = znt.ArgParsing().create_parser().parse_args(['testuser', 's', '<root/>'])
    try:
        znt.send_messages('testuser', '<b>msg</b>', False)
    except SystemExit as err:
        code = err.code
    finally:
        znt.bot, znt.get_send_id, znt.time.sleep, znt.args = saved
    assert code == 0, code
    assert bot.sent == 1, bot.sent
    assert slept == [3, 3], slept


def test_flood_wait_that_overruns_the_alert_timeout_returns_to_zabbix():
    """Zabbix kills a script media type at 40 s; a wait past the budget would
    be a timeout instead of a clean failure Zabbix retries on its schedule."""
    slept = []
    bot = _FloodedBot(floods=1, retry_after=znt.SEND_DEADLINE + 1)
    real_sleep, znt.time.sleep = znt.time.sleep, slept.append
    try:
        znt.telegram(bot.send_message, chat_id=1, text='x')
    except _StubError as err:
        assert err.error_code == 429
    else:
        raise AssertionError('a flood past the budget must be raised')
    finally:
        znt.time.sleep = real_sleep
    assert slept == [] and bot.sent == 0, (slept, bot.sent)


def test_other_telegram_errors_are_not_retried():
    slept = []
    calls = []

    def bad_request(**kwargs):
        calls.append(kwargs)
        err = _StubError('Error code: 400. Description: Bad Request')
        err.error_code, err.result_json = 400, {'ok': False, 'error_code': 400}
        raise err

    real_sleep, znt.time.sleep = znt.time.sleep, slept.append
    try:
        znt.telegram(bad_request, chat_id=1)
    except _StubError:
        pass
    finally:
        znt.time.sleep = real_sleep
    assert len(calls) == 1 and slept == [], (calls, slept)


def test_argv_survives_a_non_utf8_locale():
    """Zabbix passes UTF-8 bytes; an interpreter with no UTF-8 locale decodes
    them as ASCII with surrogateescape. The lone surrogates that produces cannot
    be encoded for the UTF-16 length measurement, so every Cyrillic alert would
    die on UnicodeEncodeError -- a lost notification. Found by rehearsing a
    dry run under LC_ALL=C, not by any test."""
    text = u'\u041f\u0440\u043e\u0431\u043b\u0435\u043c\u0430'      # "Проблема"
    mangled = text.encode('utf-8').decode('ascii', 'surrogateescape')
    try:
        mangled.encode('utf-8')
        raise AssertionError('fixture is not actually mangled')
    except UnicodeEncodeError:
        pass

    restored = znt.decode_argv([mangled])[0]
    assert restored == text, repr(restored)
    assert znt.visible_length(restored) == len(text)

    # a clean argument is passed through untouched
    assert znt.decode_argv(['{Problem}: ok'])[0] == '{Problem}: ok'


def test_secrets_can_arrive_as_media_type_parameters():
    """Zabbix resolves user macros in script media type parameters, and a
    "Secret text" macro is masked in the UI while still reaching the script.
    That is where these belong, so an argument has to win over the config."""
    parser = znt.ArgParsing().create_parser()
    parsed = parser.parse_args(
        ['user', 'subj', 'msg', 'token-test-value', '--zabbix-pass=password-test-value'])
    assert parsed.token == 'token-test-value', parsed.token
    assert parsed.zabbix_pass == 'password-test-value', parsed.zabbix_pass

    # order must not matter: Zabbix passes parameters by sortorder
    parsed = parser.parse_args(
        ['--zabbix-pass=P', 'user', 'subj', 'msg', 'T'])
    assert (parsed.token, parsed.zabbix_pass) == ('T', 'P'), parsed

    # absent means "fall back to the config", not empty string
    parsed = parser.parse_args(['user', 'subj', 'msg'])
    assert parsed.token is False and parsed.zabbix_pass is None, parsed


def test_an_old_body_template_still_renders():
    """A .cfg written before the ZNTMentions removal still carries {mentions}.
    render_message stopped supplying that key, so an existing deployment got a
    KeyError and lost every alert -- caught on the production host by the
    uncaught-exception handler, not by this suite."""
    saved = znt.body_messages
    try:
        znt.body_messages = '<b>{subject}</b>{body}{links}{tags}{mentions}'
        message = znt.render_message('subj', 'body', '', '', MORE_URL, 4096)
        assert 'subj' in message and 'body' in message, message
    finally:
        znt.body_messages = saved


def test_unexpanded_macro_parameter_falls_back_instead_of_losing_the_alert():
    """Zabbix hands over the literal '{$TG_TOKEN}' when the macro is missing or
    renamed. It is truthy, so it used to displace a working token from the
    environment and the guard below then exited 1 -- every alert lost, on a host
    that delivered fine a moment earlier. The parameter must lose to the
    fallback, not the other way round."""
    assert znt.from_parameter('{$TG_TOKEN}', 'token') is None
    assert znt.from_parameter('{$ZNT_ZABBIX_PASS}', 'pass') is None
    assert znt.from_parameter('123456:REAL-TOKEN', 'token') == '123456:REAL-TOKEN'
    assert znt.from_parameter('', 'token') == ''
    assert znt.from_parameter(False, 'token') is False        # argparse default
    # a real token can never look like a macro
    assert not '123456:AAH'.startswith('{$')


def test_the_chart_password_is_redacted_from_the_log():
    """--zabbix-pass reaches the script in argv, and main() logs the whole argv
    at DEBUG -- which is what an operator turns on when delivery is failing.
    The bot-token pattern does not match a password."""
    import logging
    fmt = znt.MaskingFormatter('%(message)s')
    line = "sys.argv: ['user', 'subj', 'msg', '--zabbix-pass=hunter2secret']"
    out = fmt.format(logging.LogRecord('t', logging.DEBUG, __file__, 1, line, None, None))
    assert 'hunter2secret' not in out, out
    assert '<password redacted>' in out, out

    # by value too, for the argv shape no pattern anticipates
    saved = znt.zabbix_api_pass
    try:
        znt.zabbix_api_pass = 'hunter2secret'
        two = "sys.argv: ['--zabbix-pass', 'hunter2secret']"
        out = fmt.format(logging.LogRecord('t', logging.DEBUG, __file__, 1, two, None, None))
        assert 'hunter2secret' not in out, out
    finally:
        znt.zabbix_api_pass = saved

    # and the redaction must not eat our own prose
    prose = '--zabbix-pass is an unexpanded macro ({$ZNT_ZABBIX_PASS})'
    out = fmt.format(logging.LogRecord('t', logging.ERROR, __file__, 1, prose, None, None))
    assert out == prose, out


def test_a_secret_that_reads_as_a_boolean_survives_the_cfg():
    """_coerce turns yes/no/on/off into booleans and digits into ints. A
    password of "no" would arrive as False and break chart auth with a generic
    "password is incorrect"."""
    src = io.open(os.path.join(ROOT, 'zbxTelegram.cfg.example'), encoding='utf-8').read()
    src = src.replace('[telegram]', '[telegram]\ntg_token = no\nzabbix_api_pass = 0')
    src = re.sub(r'(?m)^config_cache_file\s*=.*',
                 'config_cache_file = ' + os.path.join(_TMP, 'id.cache'), src)
    src = re.sub(r'(?m)^config_log_file\s*=.*',
                 'config_log_file = ' + os.path.join(_TMP, 'znt.log'), src)
    path = os.path.join(_TMP, 'boolean-secret.cfg')
    io.open(path, 'w', encoding='utf-8').write(src)
    saved = dict(os.environ)
    try:
        os.environ.pop('ZNT_TG_TOKEN', None)
        os.environ.pop('ZNT_ZABBIX_PASS', None)
        cfg = znt.load_config(path)
        assert cfg['tg_token'] == 'no', repr(cfg['tg_token'])
        assert cfg['zabbix_api_pass'] == '0', repr(cfg['zabbix_api_pass'])
    finally:
        os.environ.clear()
        os.environ.update(saved)


def test_event_without_an_item_is_still_delivered():
    """Found by the first deployment, not by review or by the suite.

    <itemid></itemid> parses to None and main() called .split() on it, so any
    event with no item lost its alert to a traceback. Missing stays an error --
    that is a broken envelope -- but empty is a legitimate way for an action to
    say "nothing here".
    """
    parsed = znt.xml_parsing(
        ENVELOPE.format(message='body').replace('<itemid>60605</itemid>',
                                                '<itemid></itemid>'))
    assert parsed is not None
    assert parsed['itemid'] == '', repr(parsed['itemid'])

    sent = {}

    def fake_send(sent_to, message, graphs_png, *args, **kwargs):
        sent['message'] = message

    real = (znt.send_messages, znt.args)
    znt.send_messages = fake_send
    znt.args = znt.ArgParsing().create_parser().parse_args(
        ['testuser', '{Problem}: no item',
         ENVELOPE.format(message='body').replace('<itemid>60605</itemid>',
                                                 '<itemid></itemid>')])
    try:
        znt.main()
    except SystemExit as err:
        assert err.code == 0, err.code
    finally:
        znt.send_messages, znt.args = real
    assert 'body' in sent.get('message', ''), sent


def test_empty_flag_reads_as_false_not_as_a_broken_envelope():
    """<graphs></graphs> disables a decoration; it must not cost the alert."""
    parsed = znt.xml_parsing(
        ENVELOPE.format(message='body').replace('<graphs>False</graphs>',
                                                '<graphs></graphs>'))
    assert parsed is not None and parsed['settings_graphs_bool'] is False
    parsed = znt.xml_parsing(
        ENVELOPE.format(message='body').replace('<graphs_period>default</graphs_period>',
                                                '<graphs_period></graphs_period>'))
    assert parsed['graphs_period'] == 'default', parsed['graphs_period']


def test_missing_config_key_falls_back_to_the_shipped_example():
    """125 config names reach the code as globals, so a key an operator left out
    would surface as a NameError halfway through building a notification --
    costing the alert rather than the setting."""
    src = io.open(os.path.join(ROOT, 'zbxTelegram.cfg.example'), encoding='utf-8').read()
    partial = '\n'.join(line for line in src.splitlines()
                        if not line.startswith('quiet_hours'))
    path = os.path.join(_TMP, 'partial.cfg')
    io.open(path, 'w', encoding='utf-8').write(partial)

    del znt.CONFIG_NOTES[:]
    cfg = znt.load_config(path)
    assert cfg['quiet_hours'] == '1-5,00:00-08:30;1-5,19:00-24:00;6-7,00:00-24:00', \
        cfg['quiet_hours']
    assert any('quiet_hours' in note for note in znt.CONFIG_NOTES), znt.CONFIG_NOTES
    del znt.CONFIG_NOTES[:]


def test_non_string_quiet_hours_does_not_crash_the_alert():
    """'quiet_hours = yes' coerces to a bool, and True.split() is an
    AttributeError that `except ValueError` would not have caught."""
    saved = znt.quiet_hours
    try:
        znt.quiet_hours = True
        assert znt.send_quietly('{Problem}: host down', WED_NIGHT) is False
    finally:
        znt.quiet_hours = saved


def test_main_delivers_a_normal_alert_within_the_caption_limit():
    """The path every ordinary alert takes, which nothing covered: envelope,
    chart, caption limit, volume. A wrong sends_photo brings defect 3 back, and
    the failure mode is a 400 and a lost notification, not a visible error.

    Also exercises a chart-bearing alert with settings tagging off, which used
    to subscript False and take every such alert down.
    """
    sent = {}

    def fake_send(sent_to, message, graphs_png, *args, **kwargs):
        sent['to'] = sent_to
        sent['message'] = message
        sent['graphs_png'] = graphs_png
        sent['disable_notification'] = kwargs.get('disable_notification')

    envelope = ENVELOPE.format(message='B' * 3000).replace(
        '<graphs>False</graphs>', '<graphs>True</graphs>')
    subject = u'{Problem} Disaster {Disaster}: ' + 'N' * 300

    real = (znt.send_messages, znt.args, znt.get_chart_png, znt.quiet_hours)
    znt.send_messages = fake_send
    znt.get_chart_png = lambda **kwargs: {'img': b'PNG', 'url': 'https://zbx.example/c'}
    znt.quiet_hours = ''          # keep the volume assertion off the wall clock
    znt.args = znt.ArgParsing().create_parser().parse_args(
        ['testuser', subject, envelope])
    try:
        znt.main()
    except SystemExit as err:
        assert err.code == 0, err.code
    finally:
        (znt.send_messages, znt.args, znt.get_chart_png, znt.quiet_hours) = real

    assert sent, 'nothing was sent at all'
    assert sent['graphs_png'], 'the chart should have been attached'
    units = len(znt.visible_text(sent['message']).encode('utf-16-le')) // 2
    assert units <= znt.tg_caption_max_symbol, units
    assert u'\U0001F6A8' in sent['message'], 'status emoji missing'
    assert sent['disable_notification'] is False, 'a problem outside quiet hours is loud'


def test_chart_fetch_failure_falls_back_to_text():
    """A frontend outage must not discard the alert with its chart."""
    sent = []

    class TextBot(object):
        def send_message(self, **kwargs):
            sent.append(kwargs['text'])

    def fail(*args, **kwargs):
        raise RuntimeError('frontend unavailable')

    real = (znt.get_cookie, znt.requests.get, znt.bot, znt.get_send_id,
            znt.bot_identity, znt.args)
    znt.get_cookie = lambda: {'zbx_session': 'test'}
    znt.requests.get = fail
    znt.bot = TextBot()  # A photo or media-group call cannot succeed here.
    znt.get_send_id = lambda recipient: 1
    znt.bot_identity = lambda: '@testbot(1)'
    try:
        for itemids in ('60605', '60605 60606'):
            envelope = ENVELOPE.format(message='alert body').replace(
                '<graphs>False</graphs>', '<graphs>True</graphs>').replace(
                '<itemid>60605</itemid>', '<itemid>{}</itemid>'.format(itemids))
            znt.args = znt.ArgParsing().create_parser().parse_args(
                ['testuser', '{Problem}: test', envelope])
            try:
                znt.main()
            except SystemExit as err:
                assert err.code == 0, err.code
    finally:
        (znt.get_cookie, znt.requests.get, znt.bot, znt.get_send_id,
         znt.bot_identity, znt.args) = real
    assert len(sent) == 2 and all('alert body' in message for message in sent), sent


# --- defect 4: unexpanded Zabbix macros in the subject ----------------------

def test_status_literal_becomes_an_emoji():
    assert znt.substitute_status_emoji('{Problem}') == u'\U0001F6A8'
    assert znt.substitute_status_emoji('{Resolved} {Disaster}') == u'\u2705 \U0001F494'


def test_unexpanded_macro_in_subject_does_not_crash():
    """alert 552765: AttributeError: 'str' object has no attribute 'CHECK'.

    str.format_map reads {$WEB_SERVICE.CHECK.INTERVAL} as attribute access on
    a field named $WEB_SERVICE, so FailSafeDict — which only covers a missing
    key — never got a chance."""
    subject = '{Problem}: Web scenario failed (interval {$WEB_SERVICE.CHECK.INTERVAL})'
    out = znt.substitute_status_emoji(subject)
    assert out.startswith(u'\U0001F6A8: '), out
    assert '{$WEB_SERVICE.CHECK.INTERVAL}' in out, out


def test_other_unexpanded_macro_shapes_are_left_alone():
    """Dotted, indexed, LLD and empty braces all used to be format_map traps."""
    for macro in ('{HOST.NAME}', '{ITEM.VALUE1}', '{#FSNAME}', '{}',
                  '{$A.B.C}', '{EVENT.TAGS.foo}'):
        out = znt.substitute_status_emoji('x ' + macro + ' y')
        assert out == 'x ' + macro + ' y', (macro, out)


# --- defect 3: Telegram counts the whole caption, not just the body ---------

MORE_URL = 'https://zbx.example/tr_events.php?triggerid=1&eventid=2'


def test_caption_limit_counts_subject_and_links_too():
    """alerts 552830/552823/552448/552446: "message caption is too long".

    body_messages_max_symbol trimmed the body to 600, but Telegram applies
    1024 to the rendered caption as a whole, so a long subject plus links
    still overflowed."""
    subject = 'A' * 400
    raw_body = 'B' * 900
    links = ' '.join('<a href="https://zbx.example/i/{}">D</a>'.format(n) for n in range(8))
    tags = '#tag_one #tag_two #tag_three'
    message = znt.render_message(subject, raw_body, links, tags, MORE_URL, 1024)
    assert znt.visible_length(message) <= 1024, znt.visible_length(message)
    assert subject in message, 'the subject must survive; the body is what gives way'
    assert 'B' in message, 'some body should remain in this case'


def test_visible_length_ignores_markup_and_counts_entities_once():
    """Telegram measures the text after HTML parsing: tags free, &amp; is 1."""
    assert znt.visible_length('<b>abc</b>') == 3
    assert znt.visible_length('a &amp; b') == 5
    assert znt.visible_length('<a href="https://long.example/very/long/url">x</a>') == 1


def test_caption_falls_back_to_plain_text_when_subject_alone_overflows():
    """Nothing left to trim: send readable plain text, not a 400."""
    message = znt.render_message('S' * 2000, '', '', '', MORE_URL, 1024)
    assert znt.visible_length(message) <= 1024, znt.visible_length(message)


def test_message_limit_is_larger_than_caption_limit():
    """Without a chart the message limit is 4096, so nothing gets trimmed."""
    raw_body = 'B' * 900
    message = znt.render_message('subject', raw_body, '', '', MORE_URL, 4096)
    assert znt.visible_length(message) <= 4096
    assert message.count('B') == znt.body_messages_max_symbol, message.count('B')


def test_body_is_cut_before_escaping_not_after():
    """Cutting escaped text can slice an entity in half ("&am"), which Telegram
    rejects as a parse error.

    Swept across paddings on purpose: at some offsets the pre-fix code happens
    to cut cleanly, and a single fixture can sit on one of those and pass
    against the bug it is named for.
    """
    for pad in range(0, 8):
        raw_body = 'x' * (znt.body_messages_max_symbol - pad) + '&&&&&&'
        message = znt.render_message('s', raw_body, '', '', MORE_URL, 4096)
        text = re.sub(r'<[^>]+>', '', message)   # drop markup, keep entities
        dangling = re.search(r'&(?!amp;|lt;|gt;|quot;|#\d)', text)
        assert not dangling, (pad, text[-80:])


# --- quiet hours ------------------------------------------------------------

# 2026-09-09 is a Wednesday, 2026-09-12 a Saturday. mon runs in UTC, which is
# the clock the media periods are written in.
import datetime  # noqa: E402

WED_NOON = datetime.datetime(2026, 9, 9, 12, 0)
WED_NIGHT = datetime.datetime(2026, 9, 9, 20, 30)
WED_EARLY = datetime.datetime(2026, 9, 9, 7, 0)
SAT_NOON = datetime.datetime(2026, 9, 12, 12, 0)


def test_zabbix_period_syntax_is_parsed():
    periods = znt.parse_periods('1-5,00:00-08:30;1-5,19:00-24:00;6-7,00:00-24:00')
    assert len(periods) == 3, periods
    assert znt.in_periods(periods, WED_NIGHT)
    assert znt.in_periods(periods, WED_EARLY)
    assert znt.in_periods(periods, SAT_NOON)
    assert not znt.in_periods(periods, WED_NOON)


def test_period_end_is_exclusive_like_zabbix():
    periods = znt.parse_periods('1-5,00:00-08:30')
    assert znt.in_periods(periods, datetime.datetime(2026, 9, 9, 8, 29))
    assert not znt.in_periods(periods, datetime.datetime(2026, 9, 9, 8, 30))


def test_invalid_periods_are_rejected_not_guessed():
    """A media "When active" period cannot wrap past midnight either, so
    19:00-08:30 is not a period Zabbix would accept and we do not invent it."""
    for bad in ('1-5,19:00-08:30', '0-5,00:00-08:30', '5-1,00:00-08:30',
                '1-5,08:30', 'nonsense', '1-8,00:00-24:00',
                '1-5,00:00-08:60',   # minute 60 silently meant 09:00
                '1-5,00:90-08:30', '1-5,25:00-26:00'):
        try:
            znt.parse_periods(bad)
        except ValueError:
            continue
        raise AssertionError('accepted an invalid period: {}'.format(bad))


def test_problem_is_loud_by_day_and_quiet_by_night():
    assert znt.send_quietly('{Problem}: host down', WED_NOON) is False
    assert znt.send_quietly('{Problem}: host down', WED_NIGHT) is True
    assert znt.send_quietly('{Problem}: host down', SAT_NOON) is True


def test_resolved_and_update_are_always_quiet():
    """Nobody needs waking for good news, and this is what keeps the pair
    together: one media type, active 24/7, volume decided per message."""
    for subject in ('{Resolved}: host up', '{Update}: acknowledged'):
        assert znt.send_quietly(subject, WED_NOON) is True, subject
        assert znt.send_quietly(subject, WED_NIGHT) is True, subject


def test_unclassifiable_subject_stays_loud():
    """"Zabbix database is not available" carries no status literal. Rare and
    severe beats quiet: an unreadable subject means something is already wrong."""
    assert znt.send_quietly('Zabbix database is not available.', WED_NIGHT) is False


def test_contradictory_status_literals_stay_loud():
    """An event name can itself contain the text "{Resolved}". Taking the first
    literal positionally would mute a live daytime Disaster."""
    subject = '{HOST.NAME}: cert {Resolved} soon {Problem} Disaster'
    assert znt.message_status(subject) is None, znt.message_status(subject)
    assert znt.send_quietly(subject, WED_NOON) is False


def test_media_type_can_carry_its_own_quiet_window():
    """A script parameter on the media type overrides the .cfg, so different
    audiences differ without a second media *period* -- periods stay 24/7,
    which is what keeps a problem and its recovery on the same channel."""
    loud_window = '3,03:00-04:00'    # never covers WED_NOON
    quiet_window = '3,11:00-13:00'   # covers WED_NOON
    assert znt.send_quietly('{Problem}: x', WED_NOON, spec=loud_window) is False
    assert znt.send_quietly('{Problem}: x', WED_NOON, spec=quiet_window) is True
    # an empty parameter means "no quiet hours", not "fall back to the .cfg"
    assert znt.send_quietly('{Problem}: x', WED_NIGHT, spec='') is False
    # and None means the parameter was not passed at all
    saved = znt.quiet_hours
    try:
        znt.quiet_hours = '1-7,00:00-24:00'
        assert znt.send_quietly('{Problem}: x', WED_NOON, spec=None) is True
    finally:
        znt.quiet_hours = saved


def test_broken_quiet_hours_config_never_silences():
    """A typo in the config must not swallow alerts."""
    saved = znt.quiet_hours
    try:
        znt.quiet_hours = '1-5,19:00-08:30'   # the invalid wrap-around form
        assert znt.send_quietly('{Problem}: host down', WED_NIGHT) is False
    finally:
        znt.quiet_hours = saved


def test_quiet_hours_needs_no_state_between_runs():
    """Same inputs, same answer: no cache, no database, no history lookup."""
    first = znt.send_quietly('{Problem}: host down', WED_NIGHT)
    assert first == znt.send_quietly('{Problem}: host down', WED_NIGHT)
    assert first is True


def test_real_action_subject_shape():
    """Exactly what actions 30/61/62 send. {{TRIGGER.SEVERITY}} is doubled on
    purpose: Zabbix expands the inner macro first, so the severity name arrives
    already wrapped in braces and picks up its own emoji."""
    subject = '{Problem} Disaster {Disaster}: db01 is unavailable'
    assert znt.message_status(subject) == 'Problem'
    assert znt.send_quietly(subject, WED_NIGHT) is True
    assert znt.send_quietly(subject, WED_NOON) is False
    rendered = znt.substitute_status_emoji(subject)
    assert rendered == u'\U0001F6A8 Disaster \U0001F494: db01 is unavailable', rendered

    resolved = '{Resolved} Disaster {Disaster} db01 is unavailable'
    assert znt.message_status(resolved) == 'Resolved'
    assert znt.send_quietly(resolved, WED_NOON) is True


def test_uncaught_exception_reaches_the_log():
    """A traceback goes to stderr and would never land in the log file, so the
    worst failure mode -- the script dying mid-delivery -- would be invisible to
    anything watching that log. Found the hard way: the empty <itemid> crash."""
    import logging

    class Grab(logging.Handler):
        def __init__(self):
            logging.Handler.__init__(self)
            self.records = []

        def emit(self, record):
            self.records.append(record)

    grab = Grab()
    logging.getLogger().addHandler(grab)
    try:
        try:
            raise ValueError('boom')
        except ValueError:
            znt.log_uncaught(*sys.exc_info())
    except SystemExit as err:
        assert err.code == 1, err.code
    finally:
        logging.getLogger().removeHandler(grab)

    assert grab.records, 'nothing was logged'
    record = grab.records[-1]
    assert record.levelno == logging.CRITICAL, record.levelname
    assert record.exc_info is not None, 'the traceback must be attached'


# --- log hygiene ------------------------------------------------------------

def test_bot_token_is_masked_in_log_output():
    """Telegram API errors carry .../bot<token>/..., and Zabbix copies our
    stdout into alerts.error. The token must not survive formatting."""
    import logging
    fmt = znt.MaskingFormatter('%(message)s')
    url = 'https://api.telegram.org/bot123456789:AAHdqTcvCH1vGWJxfSeofSAs0K5PALDsaw/sendPhoto?chat_id=-100'
    record = logging.LogRecord('t', logging.ERROR, __file__, 1, url, None, None)
    out = fmt.format(record)
    assert '<token redacted>' in out, out
    assert 'AAHdqTcvCH1vGWJxfSeofSAs0K5PALDsaw' not in out, out
    assert znt.tg_token not in fmt.format(
        logging.LogRecord('t', logging.ERROR, __file__, 1,
                          'token is ' + znt.tg_token, None, None))


def test_unwritable_log_does_not_kill_delivery():
    """A log that cannot be opened must cost a warning, not the notification."""
    import logging
    root = logging.getLogger()
    saved, level = list(root.handlers), root.level
    try:
        root.handlers = []
        znt.config_log_file = '/proc/znt-cannot-exist/znt.log'
        log = znt.System().log            # must not raise
        assert len(log.handlers) == 1, [type(h).__name__ for h in log.handlers]
    finally:
        root.handlers = saved
        root.setLevel(level)
        znt.config_log_file = os.path.join(_TMP, 'znt.log')


def _run():
    failed = 0
    for name, fn in sorted(globals().items()):
        if not name.startswith('test_'):
            continue
        try:
            fn()
        except (Exception, SystemExit) as err:
            failed += 1
            sys.stdout.write('FAIL {}: {}: {}\n'.format(name, type(err).__name__, err))
        else:
            sys.stdout.write('ok   {}\n'.format(name))
    sys.stdout.write('\n{}\n'.format('FAILED ({})'.format(failed) if failed else 'all passed'))
    return 1 if failed else 0


if __name__ == '__main__':
    sys.exit(_run())
