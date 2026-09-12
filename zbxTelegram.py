#!/usr/bin/python3
# -*- coding: utf-8 -*-
########################
#    Sokolov Dmitry    #
# xx.sokolov@gmail.com #
#  https://t.me/ZbxNTg #
########################
# https://github.com/xxsokolov/Zabbix-Notification-Telegram
__author__ = "Sokolov Dmitry"
__maintainer__ = "Sokolov Dmitry"
__license__ = "MIT"
import telebot
from telebot import apihelper
from telebot.types import InputMediaPhoto
from zbxTelegram_files.classes.argparser import ArgParsing
import xmltodict
import requests
import urllib3
import re
import sys
import os
import json
import logging
import html
import configparser
import datetime


# A Telegram API error carries the full request URL, .../bot<token>/sendPhoto?...
# Zabbix copies whatever the alertscript writes to stdout into alerts.error, so an
# unmasked token ends up in the database and on the frontend as well as in the log.
TOKEN_RE = re.compile(r'\d{6,}:[A-Za-z0-9_-]{30,}')
# The chart account's password arrives as a command-line parameter, and main()
# logs the whole argv at DEBUG - which is exactly what an operator turns on when
# deliveries are failing. TOKEN_RE does not match a password shape.
PASSWORD_RE = re.compile(r'(--zabbix-pass=)\S+')


class MaskingFormatter(logging.Formatter):
    """Masks bot tokens in the final text, tracebacks included."""

    def format(self, record):
        text = TOKEN_RE.sub('<token redacted>', super().format(record))
        text = PASSWORD_RE.sub(r'\1<password redacted>', text)
        # Also by value, which catches the forms no pattern anticipates - the
        # two-element argv, a traceback frame, a requests error. Short secrets
        # are left alone: replacing "no" everywhere would mangle the prose.
        secret = globals().get('zabbix_api_pass') or ''
        if len(secret) >= 8:
            text = text.replace(secret, '<password redacted>')
        return text


class System:
    def __init__(self, debug=False):
        # configuring log
        if debug:
            self.log_level = logging.DEBUG
        else:
            self.log_level = logging.INFO

        # A failing handler makes logging print the *unformatted* record to
        # stderr, which would route an unmasked token straight into Zabbix's
        # alerts.error. There is nothing useful to do with that report anyway.
        logging.raiseExceptions = False

        log_format = MaskingFormatter(
            '[%(asctime)s] - PID:%(process)s - %(funcName)s() - %(filename)s:%(lineno)d - %(levelname)s: %(message)s')
        self.log = logging.getLogger()
        self.log.setLevel(self.log_level)

        # writing to stdout
        stdout_handler = logging.StreamHandler(sys.stdout)
        # stdout_handler = logging.StreamHandler(codecs.getwriter("utf-8")(sys.stdout.detach()))
        stdout_handler.setLevel(self.log_level)
        stdout_handler.setFormatter(log_format)
        self.log.addHandler(stdout_handler)

        # writing to file. A log that cannot be opened must never cost a
        # notification, so fall back to stdout instead of dying here.
        try:
            log_dir = os.path.dirname(config_log_file)
            if log_dir:
                # exist_ok, not isdir-then-create: one process per alert means a
                # burst races here, and every loser would drop to stdout only.
                os.makedirs(log_dir, mode=0o750, exist_ok=True)
            # encoding matters: the messages carry emoji, and a C-locale daemon
            # would otherwise raise UnicodeEncodeError on every write.
            file_handler = logging.FileHandler(filename=config_log_file, mode='a',
                                               encoding='utf-8')
            try:
                # The log carries chat ids and message bodies; 0644 was how the
                # old znt.log ended up world-readable. logrotate keeps 0640.
                os.chmod(config_log_file, 0o640)
            except OSError:
                pass
            file_handler.setLevel(self.log_level)
            file_handler.setFormatter(log_format)
            self.log.addHandler(file_handler)
        except (IOError, OSError) as err:
            self.log.warning('Cannot write to %s (%s); logging to stdout only.',
                             config_log_file, err)


CONFIG_NOTES = []


# A password of "no" must stay the string "no", not become False, and a token
# is never a number. These three keep whatever the file says.
SECRET_KEYS = ('tg_token', 'zabbix_api_pass', 'tg_proxy_url')


def _unquote(raw):
    """Strip the optional double quotes, without guessing at a type."""
    value = raw.strip()
    if len(value) >= 2 and value[0] == '"' and value[-1] == '"':
        return value[1:-1]
    return value


def _coerce(raw):
    """.cfg text -> python value. Quoted keeps whitespace, yes/no -> bool, digits -> int."""
    value = raw.strip()
    if len(value) >= 2 and value[0] == '"' and value[-1] == '"':
        return value[1:-1]
    low = value.lower()
    if low in ('true', 'yes', 'on'):
        return True
    if low in ('false', 'no', 'off'):
        return False
    if re.match(r'^-?\d+$', value):
        return int(value)
    return value


def _read_cfg(path):
    """Parse one .cfg into flat name -> value, or None if it cannot be read."""
    parser = configparser.RawConfigParser()
    parser.optionxform = str  # emoji keys are case-sensitive ('Not classified')
    if not parser.read(path, encoding='utf-8'):
        return None
    cfg = {}
    for section in parser.sections():
        if section == 'emoji':
            cfg['zabbix_status_emoji_map'] = dict(parser.items(section))
            continue
        for key, raw in parser.items(section):
            cfg[key] = _unquote(raw) if key in SECRET_KEYS else _coerce(raw)
    return cfg


def load_config(path=None):
    """Read the .cfg into a flat dict of names the script uses as globals.

    Sections group keys for readers only; each key is already the exact global
    name, so no mapping table can drift out of sync with the code. Secrets are
    never read from the file - the config is a public artifact.
    """
    here = os.path.dirname(os.path.realpath(__file__))
    path = path or os.environ.get('ZNT_CONFIG') or os.path.join(here, 'zbxTelegram.cfg')
    cfg = _read_cfg(path)
    if cfg is None:
        sys.stderr.write('znt: cannot read config {}\n'.format(path))
        sys.exit(1)

    # The shipped example is the schema. These names reach the code as globals,
    # so a key the operator left out would otherwise surface as a NameError
    # halfway through building a notification - costing the alert, not just the
    # setting. Fall back to the shipped value and say so instead.
    example = os.path.join(here, 'zbxTelegram.cfg.example')
    defaults = _read_cfg(example)
    if not defaults:
        CONFIG_NOTES.append(
            '{} is not installed beside the script, so a key missing from {} '
            'will fail as a NameError mid-delivery instead of falling back.'
            .format(example, path))
    if defaults:
        missing = sorted(key for key in defaults if key not in cfg)
        if missing:
            CONFIG_NOTES.append(
                '{} does not define {}; using the values from {}.'.format(
                    path, ', '.join(missing), example))
            for key in missing:
                cfg[key] = defaults[key]

    # yes/no coercion could have turned this into a bool; parse_periods wants text.
    cfg['quiet_hours'] = str(cfg.get('quiet_hours', '') or '')

    # Secrets, in order of precedence: environment, then whatever the .cfg holds.
    # A media type parameter beats both, but that is applied later, once argv is
    # parsed. The .cfg is the last resort rather than a forbidden place: putting
    # them there is the only route that keeps them out of `ps` and out of the
    # Zabbix database, and an operator on a shared host may want exactly that.
    cfg['tg_token'] = os.environ.get('ZNT_TG_TOKEN') or cfg.get('tg_token', '')
    cfg['zabbix_api_pass'] = (os.environ.get('ZNT_ZABBIX_PASS')
                              or cfg.get('zabbix_api_pass', ''))
    cfg['zabbix_api_login'] = (os.environ.get('ZNT_ZABBIX_LOGIN')
                               or cfg.get('zabbix_api_login', ''))
    cfg['tg_proxy_server'] = {'https': (os.environ.get('ZNT_TG_PROXY_URL')
                                        or cfg.get('tg_proxy_url', ''))}
    return cfg


# ponytail: 600 lines already read these as module globals, so inject them and
# leave the call sites alone. Ceiling: static checkers stop seeing the names
# (ruff reports ~125 F821 here). A key the .cfg omits is covered by the fallback
# to the shipped example above; what is left uncovered is a key misspelled in
# BOTH files, which still surfaces as a NameError mid-delivery rather than at
# lint time. Upgrade path if that ever bites: rewrite the call sites to cfg[...].
globals().update(load_config())

def decode_argv(argv):
    """Undo surrogateescape so the arguments are text again.

    Zabbix hands the script UTF-8 bytes. An interpreter started without a UTF-8
    locale decodes them as ASCII with surrogateescape, and the lone surrogates
    that produces cannot be encoded again - not for the UTF-16 length
    measurement, and not for delivery. Every non-English alert would die on
    UnicodeEncodeError, which is a lost notification, so put the text back.

    Keyed on the surrogates themselves rather than on the locale: an argument
    that already encodes cleanly is left exactly as it is.
    """
    decoded = []
    for arg in argv:
        try:
            arg.encode('utf-8')
        except UnicodeEncodeError:
            arg = os.fsencode(arg).decode('utf-8', 'replace')
        decoded.append(arg)
    return decoded


args = ArgParsing().create_parser().parse_args(decode_argv(sys.argv[1:]))
loggings = System(config_debug_mode if not args.debug else True).log
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

def log_uncaught(exc_type, exc_value, exc_traceback):
    """An uncaught traceback goes to stderr, which never reaches the log file -
    so the one failure mode that matters most would be invisible to anything
    watching the log. Record it, masked, then exit non-zero as before."""
    if issubclass(exc_type, KeyboardInterrupt):
        sys.__excepthook__(exc_type, exc_value, exc_traceback)
        return
    loggings.critical('Unhandled exception, notification not delivered.',
                      exc_info=(exc_type, exc_value, exc_traceback))
    sys.exit(1)


sys.excepthook = log_uncaught

for _note in CONFIG_NOTES:
    loggings.warning(_note)

# Secrets may arrive as media type parameters, which is where Zabbix keeps this
# kind of thing: a global macro of type "Secret text" is masked in the UI, is
# exported as its own name rather than its value, and is resolved for script
# parameters. An argument wins over the config, which still reads the
# environment, so a host configured the old way keeps working.
def from_parameter(value, name):
    """A media type parameter, unless Zabbix failed to resolve the macro.

    An unresolved macro arrives as the literal '{$NAME}', which is truthy and
    would otherwise displace a perfectly good value from the environment or the
    config - turning a missing macro into total loss on a host that was
    delivering a moment ago. Say so and let the fallback stand.
    """
    if value and value.startswith('{$'):
        # WARNING, not ERROR: the delivery is about to succeed on the fallback,
        # and the log trigger that watches this file for ERROR would otherwise
        # page someone about a message that arrived. If no fallback exists, the
        # token guard below logs ERROR and that is the real failure.
        loggings.warning('%s is an unexpanded macro (%s); Zabbix did not '
                         'resolve it. Falling back to the environment or the '
                         'config.', name, value)
        return None
    return value


tg_token = from_parameter(args.token, 'The token parameter') or tg_token
zabbix_api_pass = (from_parameter(args.zabbix_pass, '--zabbix-pass')
                   or zabbix_api_pass)
if not tg_token or tg_token.startswith('{$'):
    loggings.error('Telegram token is not set. Create the {$TG_TOKEN} global '
                   'macro and pass it as a media type parameter, or set '
                   'ZNT_TG_TOKEN in the environment.')
    sys.exit(1)
bot = telebot.TeleBot(tg_token)
# pyTelegramBotAPI already knows how to retry connection, DNS and timeout
# failures. Keep the retry budget bounded: a persistent outage must return to
# Zabbix, whose media settings perform the next attempt.
# ponytail: three one-second attempts (two retries); a longer outage belongs
# to Zabbix/network
apihelper.RETRY_ON_ERROR = True
apihelper.RETRY_ENGINE = 1
apihelper.MAX_RETRIES = 3
apihelper.RETRY_TIMEOUT = 1
if tg_proxy:
    if not tg_proxy_server.get('https'):
        # requests treats {'https': ''} as no proxy at all, so this would go
        # direct without a word. Say it, then deliver anyway.
        loggings.error('tg_proxy is on but ZNT_TG_PROXY_URL is not set; '
                       'connecting to Telegram directly.')
    else:
        apihelper.proxy = tg_proxy_server


def xml_parsing(data):
    try:
        # Zabbix prepends a server-generated plaintext banner (e.g.
        # "NOTE: Escalation canceled: ...\nLast message sent:") ahead of the XML
        # body when an escalation is canceled (trigger/host disabled mid-escalation).
        # Strip anything before the XML root so xmltodict can still parse it, and
        # keep the banner to prepend to the delivered message.
        znt_prefix = ''
        if data is not None:
            _m = data.find('<?xml')
            if _m == -1:
                _m = data.find('<root')
            if _m > 0:
                znt_prefix = data[:_m].strip()
                data = data[_m:]
        parsed = dict(xmltodict.parse(data, process_namespaces=True)['root'])
    except Exception as err:
        # Not fatal: Zabbix sends its own internal alerts (a dead database, for
        # one) as plain text with no envelope at all. Losing those is exactly
        # the moment monitoring matters most, so the caller delivers them raw.
        loggings.warning("No XML envelope in the message (%s); falling back to "
                         "plain text delivery.", err, exc_info=config_exc_info)
        return None

    try:
        message = parsed['body']['messages'] or ''
        if znt_prefix:
            message = znt_prefix + '\n' + message

        settings = parsed['settings']

        # An element written as <itemid></itemid> parses to None, and callers
        # go straight on to .split() it. Missing is still an error - that is a
        # broken envelope - but empty is a legitimate way to say "nothing here".
        def text(name, default=''):
            return settings[name] if settings[name] is not None else default

        # Every flag maps X -> settings_X_bool, so name them once. This used to
        # be 15 eval() calls on text straight out of the action XML.
        flags = ('graphs', 'graphlinks', 'triggerlinks', 'hostlinks', 'acklinks',
                 'eventlinks', 'eventtag', 'eventidtag', 'itemidtag', 'triggeridtag',
                 'actionidtag', 'hostidtag')
        parsed_settings = dict(
            ('settings_{}_bool'.format(flag), text(flag).capitalize() == 'True')
            for flag in flags)
        parsed_settings.update(
            title=text('title'), message=message, eventtags=text('eventtags'),
            graphs_period=text('graphs_period', 'default'), host=text('host'),
            itemid=text('itemid'), triggerid=text('triggerid'),
            triggerurl=text('triggerurl'), eventid=text('eventid'),
            actionid=text('actionid'), hostid=text('hostid'))
        return parsed_settings

    except Exception as err:
        # The document parsed, so this is our own action XML being wrong, not a
        # plain-text alert. Delivering the raw document as the alert text would
        # report success for something nobody can read, so fail visibly and let
        # Zabbix record it and retry.
        loggings.error("XML envelope parsed but is missing fields (%s); check "
                       "the action's message template.", err,
                       exc_info=config_exc_info), exit(1)


def get_cookie():
    data_api = {"name": zabbix_api_login,"password": zabbix_api_pass,"enter": "Sign in"}
    req_cookie = requests.post(zabbix_api_url, data=data_api, verify=False, timeout=5)
    cookie = req_cookie.cookies
    req_cookie.close()
    if not any(_ in cookie for _ in ['zbx_session', 'zbx_sessionid']):
        loggings.error(
            'User authorization failed: {} ({})'.format('Login name or password is incorrect.', zabbix_api_url))
        return False
    return cookie


def get_chart_png(itemid, graff_name, period=None):
    try:
        cookies = get_cookie()
        if cookies:
            response = requests.get(zabbix_graph_chart.format(
                name=graff_name,
                itemid=itemid,
                zabbix_server=zabbix_api_url,
                range_time=period),
                cookies=cookies,
                verify=False,
                timeout=10)

            return dict(img=response.content, url=response.url)
        else:
            return dict(img=None, url=None)
    except Exception as err:
        loggings.error("Exception occurred: {}".format(err), exc_info=config_exc_info), exit(1)


def create_tags_list(_bool=False, tag=None, _type=None):
    """Turn a Zabbix event-tag string into hashtags."""
    if not _bool:
        return False
    tags_list = []
    if tag and re.search(r'\w', tag):
        for item in tag.split(', '):
            if not item:
                tags_list.append(body_messages_tags_no)
                continue
            if item.find(':') != -1:
                name, value = re.split(r':+', item, maxsplit=1)
                tags_list.append('#{tag}_{value}'.format(
                    tag=(_type or '') + re.sub(r"\W+", "_", name),
                    value=re.sub(r"\W+", "_", value)))
            else:
                for word in item.split() or [item]:
                    tags_list.append('#{tag}'.format(
                        tag=(_type or '') + re.sub(r"\W+", "_", word)))
    else:
        tags_list.append(body_messages_tags_no)
    return body_messages_tags_delimiter.join(tags_list)


def create_links_list(_bool=None, url=None, _type=None, url_list=None):
    try:
        if _bool:
            if url and (re.search(r'\w', url)):
                return body_messages_url_template.format(
                    url=html.escape(url, quote=True), icon=_type)
            else:
                return body_messages_url_emoji_no_url
        elif url_list:
            return url_list
        else:
            return False
    except ValueError:
        return body_messages_url_emoji_no_url


def _open_cache():
    """Create the chat-id cache with permissions suitable for private ids."""
    created = not os.path.exists(config_cache_file)
    fd = os.open(config_cache_file, os.O_CREAT | os.O_RDWR, 0o640)
    os.close(fd)
    os.chmod(config_cache_file, 0o640)
    return created


def get_cache(title):
    read_cache = None
    try:
        created = _open_cache()
    except OSError as err:
        loggings.error("Exception occurred: {}".format(err), exc_info=config_exc_info)
    else:
        read_cache = open(config_cache_file, 'r').read()
        if created:
            loggings.info("Cache file created in {}".format(config_cache_file))

    if read_cache:
        cache = json.loads(read_cache)

        for name, value in cache.items():
            if title == name:
                return value['id']
    else:
        return False


def set_cache(title, send_id, sent_type, cache=None, update=None):
    _open_cache()
    f = open(config_cache_file, 'r+')
    r = f.read()
    if r:
        cache = json.loads(r)
    if not cache:
        cache = {title: dict(type=str(sent_type), id=str(send_id))}
    else:
        if not update:
            cache[title] = dict(type=str(sent_type), id=str(send_id))
        else:
            cache[title] = dict(type=str(sent_type), id=str(send_id), old=str(update))
    f.seek(0)
    f.write(json.dumps(cache,sort_keys=True, ensure_ascii=False, indent=4))
    f.truncate()  # a shorter dict used to leave the old tail behind, breaking JSON
    f.close()
    if update:
        loggings.info("Updated id for {} ({}): old '{}' -> new '{}' in cache file".format(
            title, sent_type, update, send_id))
    else:
        loggings.info("Add new id {} for {} ({}) in cache file".format(send_id, title, sent_type))
    return True


def migrate_group_id(sent_to, sent_id, err):
    for key, value in json.loads(err.result.text).items():
        if key == 'parameters' and value['migrate_to_chat_id']:
            loggings.warning("Group chat was upgraded to a supergroup chat ({})".format(value['migrate_to_chat_id']),
                             exc_info=config_exc_info)
            set_cache(sent_to, value['migrate_to_chat_id'], 'supergroup', update=sent_id)


def get_send_id(send_to):
    try:
        chat = None
        if send_to and re.search(r'\{[^}]*\}', send_to):
            raise ValueError(
                'Recipient is an unexpanded Zabbix macro ({}). The "Send to" '
                'field of that user media is empty, so Zabbix passed the macro '
                'through verbatim; set it in Zabbix.'.format(send_to))
        if re.search('^[0-9]+$', send_to) or re.search('^-[0-9]+$', send_to):
            return send_to
        elif re.search('^@+[a-zA-Z0-9_]{5,}$', send_to):
            send_to = send_to.replace("@", "")
        elif not send_to:
            raise ValueError('Username or groupname is not specified. You can use for username '
                             '@[a-z,A-Z,0-9 and underscores] and for groupname any characters. ')

        send_id = get_cache(send_to)

        if send_id:
            return send_id

        loggings.info("Telegram API: method getUpdate: started")
        get_updates_list = bot.get_updates(timeout=10)
        sum_del_update_id = 0
        while len([value.update_id for value in get_updates_list]) >= 100:
            sum_del_update_id += len([value.update_id for value in get_updates_list])
            get_updates_list = bot.get_updates(timeout=10, offset=max([value.update_id for value in get_updates_list]))

        if sum_del_update_id > 0:
            loggings.info("In getUpdate list was cleared {} messages. Submitted for processing {}.".format(
                sum_del_update_id, len([value.update_id for value in get_updates_list])))

        for line in get_updates_list:
            if line.message:
                chat = line.message.chat
            elif line.edited_message:
                chat = line.edited_message.chat
            elif line.channel_post:
                chat = line.channel_post.chat

            if chat.type in ["group", "supergroup"] and chat.title and chat.title == send_to:
                if not send_id:
                    set_cache(send_to, chat.id, chat.type)
                bot.get_updates(timeout=10, offset=-1)
                return chat.id

            if chat.type in ["channel"] and chat.title and chat.title == send_to:
                if not send_id:
                    set_cache(send_to, chat.id, chat.type)
                bot.get_updates(timeout=10, offset=-1)
                return chat.id

            if chat.type in ["private"] and chat.username == send_to.replace("@", ""):
                if not send_id:
                    set_cache(send_to, chat.id, chat.type)
                bot.get_updates(timeout=10, offset=-1)
                return chat.id

        raise ValueError('Username or groupname not found in the cache file. No access occurred or bot is not added to '
                         'group "{sendto}" (Add bot group and/or send message to {bot})'.format(
            bot=bot_identity(),
            sendto=send_to))
    except Exception as err:
        loggings.error("Exception occurred: {}".format(err), exc_info=config_exc_info), exit(1)


BOT_IDENTITY = []


def bot_identity():
    """getMe once per process, not twice per delivered message."""
    if not BOT_IDENTITY:
        me = bot.get_me()
        BOT_IDENTITY.append('@{}({})'.format(me.username, me.id))
    return BOT_IDENTITY[0]


def send_messages(sent_to, message, graphs_png, disable_notification=False):
    if args.dry_run:
        # Everything before this ran for real: config, envelope, tags, links, the
        # chart fetch, the trimming and the volume decision. Skipped along with
        # the Telegram call is recipient resolution - get_send_id() below reaches
        # Telegram's getUpdates for a name, so a rehearsal cannot prove the chat
        # is deliverable, only what would have been sent to it.
        loggings.info('DRY RUN: to %s, %s, %s visible characters, notification %s',
                      sent_to,
                      'photo' if graphs_png else 'text',
                      visible_length(message),
                      'off' if disable_notification else 'on')
        text = message + '\n'
        try:
            sys.stdout.write(text)
        except UnicodeEncodeError:
            # Every subject carries emoji and a rehearsal is often run under the
            # C locale, where stdout is ASCII. Show the message, not a traceback.
            encoding = getattr(sys.stdout, 'encoding', None) or 'ascii'
            sys.stdout.write(text.encode(encoding, 'replace').decode(encoding))
        return  # not exit(0): the test-message path below reports via its exit code
    try:
        sent_id = get_send_id(sent_to)
        if message and sent_to:
            if graphs_png and isinstance(graphs_png, list):
                try:
                    graphs_png[0].caption = message
                    graphs_png[0].parse_mode = "HTML"
                    bot.send_media_group(chat_id=sent_id, media=graphs_png, disable_notification=disable_notification)
                except apihelper.ApiException as err:
                    if 'migrate_to_chat_id' in err.result.text:
                        migrate_group_id(sent_to, sent_id, err)
                        send_messages(sent_to, message, graphs_png, disable_notification)
                    else:
                        loggings.error("Exception occurred in Api Telegram: {}".format(err), exc_info=config_exc_info),
                        exit(1)
                except Exception as err:
                    loggings.error("Exception occurred: {}".format(err), exc_info=config_exc_info),exit(1)
                else:
                    loggings.info('Bot {bot} send media group to "{sent_to}" ({sent_id}).'.format(
                        sent_to=sent_to, sent_id=sent_id, bot=bot_identity()))
                    exit(0)
            elif graphs_png and graphs_png.get('img'):
                try:
                    bot.send_photo(chat_id=sent_id, photo=graphs_png.get('img'), caption=message,
                                   parse_mode="HTML",
                                   disable_notification=disable_notification)
                except apihelper.ApiException as err:
                    if 'migrate_to_chat_id' in err.result.text:
                        migrate_group_id(sent_to, sent_id, err)
                        send_messages(sent_to, message, graphs_png, disable_notification)
                    elif 'IMAGE_PROCESS_FAILED' in err.result.text:
                        bot.send_photo(chat_id=sent_id, photo=open(
                              file='{0}/zbxTelegram_files/error_send_photo.png'.format(
                                  os.path.dirname(os.path.realpath(__file__))),
                              mode='rb').read(), caption=message, parse_mode="HTML",
                                       disable_notification=disable_notification)
                    else:
                        loggings.error("Exception occurred in Api Telegram: {}".format(err), exc_info=config_exc_info),
                        exit(1)
                except Exception as err:
                    loggings.error("Exception occurred: {}".format(err), exc_info=config_exc_info),exit(1)
                else:
                    loggings.info('Bot {bot} send photo to "{sent_to}" ({sent_id}).'.format(
                        sent_to=sent_to, sent_id=sent_id, bot=bot_identity()))
            else:
                try:
                    bot.send_message(chat_id=sent_id, text=message, parse_mode="HTML",
                                     disable_web_page_preview=True,
                                     disable_notification=disable_notification)
                except apihelper.ApiException as err:
                    if 'migrate_to_chat_id' in err.result.text:
                        migrate_group_id(sent_to, sent_id, err)
                        send_messages(sent_to, message, graphs_png, disable_notification)
                    else:
                        loggings.error("Exception occurred in Api Telegram: {}".format(err), exc_info=config_exc_info)
                        exit(1)
                except Exception as err:
                    loggings.error("Exception occurred: {}".format(err), exc_info=config_exc_info), exit(1)
                else:
                    loggings.info('Bot {bot} send message to "{sent_to}" ({sent_id}).'.format(
                        sent_to=sent_to, sent_id=sent_id, bot=bot_identity()))
                    exit(0)
    except Exception as err:
        loggings.error("Exception occurred: {}".format(err), exc_info=config_exc_info), exit(1)


def set_period_day_hour(seconds):
    seconds = int(seconds)
    days, seconds = divmod(seconds, 86400)
    hours, seconds = divmod(seconds, 3600)
    minutes, seconds = divmod(seconds, 60)
    if days > 0:
        return '{}d {}h'.format(days, hours) if hours > 0 else '{}d'.format(days)
    elif hours > 0:
        return '{}h {}m'.format(hours, minutes) if minutes > 0 else '{}h'.format(hours)
    elif minutes > 0:
        return '{}m'.format(minutes)


def parse_periods(spec):
    """Zabbix time period syntax: d-d,hh:mm-hh:mm, ';'-separated.

    Days are 1-7, Monday to Sunday, and the end must come after the start -
    the same rules a media "When active" period obeys, so the quiet window and
    the media window can be read side by side. A period cannot wrap past
    midnight, so "19:00-08:30" is written as two periods, not invented here.
    """
    periods = []
    for chunk in spec.split(';'):
        chunk = chunk.strip()
        if not chunk:
            continue
        match = re.match(r'^(\d)(?:-(\d))?,(\d{1,2}):(\d{2})-(\d{1,2}):(\d{2})$', chunk)
        if not match:
            raise ValueError('not a Zabbix time period: {!r}'.format(chunk))
        first_day = int(match.group(1))
        last_day = int(match.group(2) or match.group(1))
        start_minute, end_minute = int(match.group(4)), int(match.group(6))
        start = int(match.group(3)) * 60 + start_minute
        end = int(match.group(5)) * 60 + end_minute
        # Check the fields, not just the total: "00:00-08:60" would otherwise
        # pass as 09:00 and widen the silence past what was written.
        if (not 1 <= first_day <= last_day <= 7
                or start_minute > 59 or end_minute > 59
                or not 0 <= start < end <= 24 * 60):
            raise ValueError('not a Zabbix time period: {!r}'.format(chunk))
        periods.append((first_day, last_day, start, end))
    return periods


def in_periods(periods, when):
    """Zabbix treats the end of a period as exclusive; so do we."""
    day = when.isoweekday()
    minute = when.hour * 60 + when.minute
    return any(first_day <= day <= last_day and start <= minute < end
               for first_day, last_day, start, end in periods)


def message_status(subject):
    """The Zabbix actions put {Problem}, {Resolved} or {Update} in the subject.

    That literal is the whole type signal, which is why no state is needed.
    """
    # One literal classifies; none or two do not. An event name can itself
    # contain the text "{Resolved}", and taking the first match positionally
    # would mute a live daytime problem.
    found = set(re.findall(r'\{(Problem|Resolved|Update)\}', subject or ''))
    return found.pop() if len(found) == 1 else None


def send_quietly(subject, when=None, spec=None):
    """Decide this one message's volume. Nothing is remembered between runs.

    Volume is a property of a message, not of a channel. Modelling it as
    routing - a second media type active only at night - is what broke the
    pairing of events: a recovery is addressed to whoever received the
    problem, so a problem received at night had its daytime resolve dropped
    with no alert row created at all.

    Resolve and update are never urgent. A problem is loud unless we are
    inside the quiet window. `spec` lets a media type carry its own window as
    a script parameter, so different audiences can differ without a second
    media period - periods stay 24/7, which is what keeps events paired.
    A subject with no status literal stays loud:
    that class is rare and severe (a dead Zabbix database announces itself
    in plain text), and an unreadable subject already means something is off.
    """
    status = message_status(subject)
    if status in ('Resolved', 'Update'):
        return True
    if status is None:
        loggings.info("No status literal in the subject; sending it loudly.")
        return False
    spec = quiet_hours if spec is None else spec
    if not spec:
        return False
    try:
        periods = parse_periods(str(spec))
    except Exception as err:
        loggings.error("quiet_hours is not a valid Zabbix time period (%s); "
                       "sending loudly rather than risk silencing an alert.", err)
        return False
    return in_periods(periods, when or datetime.datetime.now())


def substitute_status_emoji(subject):
    """Swap the {Problem}/{Resolved}/{Update}/severity literals the Zabbix
    actions put in the subject for their emoji.

    Not str.format_map: it reads {$WEB_SERVICE.CHECK.INTERVAL} as attribute
    access on a field named $WEB_SERVICE and raises AttributeError, so
    FailSafeDict - which only ever covered a missing key - could not help.
    A brace group that is not a known status is left exactly as it stands,
    which is also the honest thing to show for a macro Zabbix failed to expand.
    """
    return re.sub(r'\{([^{}]*)\}',
                  lambda m: zabbix_status_emoji_map.get(m.group(1), m.group(0)),
                  subject or '')


def visible_text(message):
    """The text Telegram sees: markup gone, entities resolved. Strip tags first
    and unescape second, so escaped text in the body is never taken for markup."""
    return html.unescape(re.sub(r'<[^>]+>', '', message))


def visible_length(message):
    """Length Telegram measures - UTF-16 code units, not python characters.

    Every severity emoji we inject is non-BMP and therefore counts as 2 there
    and 1 here. Counting characters undercounts, and since the trimmer stops
    exactly at the limit, an emoji-bearing caption lands just past it and comes
    back "caption is too long" - the very defect this function exists to close.
    """
    return len(visible_text(message).encode('utf-16-le')) // 2


def render_message(subject, raw_body, links, tags, more_url, limit):
    """Assemble the message, trimming the body until the rendered whole fits.

    `subject`, `links` and `tags` arrive already escaped/rendered;
    `raw_body` is unescaped so it can be cut before escaping - cutting escaped
    text slices entities in half ("&am") and Telegram then rejects the parse.

    Telegram applies its limit to the entire caption, so trimming only the body
    to body_messages_max_symbol still produced "message caption is too long"
    whenever the subject, links and tags pushed the total past 1024.
    """
    raw_body = raw_body or ''   # an empty <messages/> parses to None

    def assemble(budget):
        if len(raw_body) > budget:
            marker = (' <a href="{}">...</a>'.format(html.escape(more_url, quote=True))
                      if more_url else ' ...')
            body = html.escape(raw_body[:budget]) + marker
        else:
            body = html.escape(raw_body)
        # mentions='' keeps a body_messages template written before the
        # ZNTMentions removal working: an operator's existing .cfg still
        # carries {mentions}, and a KeyError here costs the notification.
        return body_messages.format(subject=subject,
                                    body='\n\n' + body if body else '',
                                    links='\n' + links if links else '',
                                    tags='\n\n' + tags if tags else '',
                                    mentions='')

    budget = body_messages_max_symbol if body_messages_cut_symbol else len(raw_body)
    budget = min(budget, len(raw_body))
    while True:
        message = assemble(budget)
        excess = visible_length(message) - limit
        if excess <= 0 or budget == 0:
            break
        # Strictly decreasing, so this terminates; a second pass covers the few
        # characters the " ..." marker adds when the cut is introduced.
        budget = max(0, budget - excess)

    if excess > 0:
        loggings.warning("Message is over %s characters with no body left; "
                         "sending it as plain text.", limit)
        plain = visible_text(message)[:limit]
        while plain and len(plain.encode('utf-16-le')) // 2 > limit:
            plain = plain[:-1]
        return html.escape(plain)
    if budget < len(raw_body):
        loggings.info("Message body trimmed to %s characters to fit the %s "
                      "character limit.", budget, limit)
    return message


def main():
    graph_period = None
    loggings.info("Send to {} action: {}".format(args.username, args.subject))
    loggings.debug("sys.argv: {}".format(sys.argv[1:]))
    loggings.debug("Send to {}\naction: {}\nxml: {}".format(args.username, args.subject, args.messages))

    if args.subject in ['Test subject', 'test', 'Тестовая тема'] or args.messages in \
            ['This is the test message from Zabbix', 'test', 'Это тестовое сообщение от Zabbix']:
        if get_cookie():
            loggings.info('Connection check passed ({})'.format(zabbix_api_url))
            test_graph_file = '{0}/zbxTelegram_files/test.png'
            error_code = 0
        else:
            test_graph_file = '{0}/zbxTelegram_files/error_send_photo.png'
            error_code = 1

        send_messages(sent_to=args.username, message='🚨 Test 🚽💩: Test message\n'
                                                     'Host: testhost [192.168.0.0]\n'
                                                     'Last value: test (10:00:00)\n'
                                                     'Duration: 1m\n'
                                                     'Description: This message is generated with test data. '
                                                     'specify as the topic and / or zabbix\n\n'
                                                     '#Test, #eid_130144443, #iid_60605, #tid_39303, #aid_22',
                      graphs_png=dict(
                          img=open(
                              file=test_graph_file.format(os.path.dirname(os.path.realpath(__file__))),
                              mode='rb').read()))
        exit(error_code)

    data_zabbix = xml_parsing(args.messages)

    if data_zabbix is None:
        # No envelope, so no tags, links or chart to build - send what we got.
        send_messages(
            args.username,
            render_message(html.escape(substitute_status_emoji(args.subject)),
                           args.messages or '', '', '',
                           more_url='', limit=tg_message_max_symbol),
            graphs_png=False,
            disable_notification=send_quietly(args.subject, spec=args.quiet_hours))
        exit(0)

    event_tags = create_tags_list(
        _bool=True if data_zabbix.get('settings_eventtag_bool') and body_messages_tags_event else False,
        tag=data_zabbix['eventtags'], _type=None)
    eventid_tags = create_tags_list(
        _bool=True if data_zabbix.get('settings_eventidtag_bool') and body_messages_tags_eventid else False,
        tag=data_zabbix['eventid'], _type=body_messages_tags_prefix_eventid)
    itemid_tags = create_tags_list(
        _bool=True if data_zabbix.get('settings_itemidtag_bool') and body_messages_tags_itemid else False,
        tag=' '.join([item_id for item_id in data_zabbix['itemid'].split() if re.findall(r"\d+", item_id)]),
        _type=body_messages_tags_prefix_itemid)
    triggerid_tags = create_tags_list(
        _bool=True if data_zabbix.get('settings_triggeridtag_bool') and body_messages_tags_triggerid else False,
        tag=data_zabbix['triggerid'], _type=body_messages_tags_prefix_triggerid)
    actionid_tags = create_tags_list(
        _bool=True if data_zabbix.get('settings_actionidtag_bool') and body_messages_tags_actionid else False,
        tag=data_zabbix['actionid'], _type=body_messages_tags_prefix_actionid)
    hostid_tags = create_tags_list(
        _bool=True if data_zabbix.get('settings_hostidtag_bool') and body_messages_tags_hostid else False,
        tag=data_zabbix['hostid'], _type=body_messages_tags_prefix_hostid)
    tags_list = []
    tags_list.append(event_tags) if event_tags else None
    tags_list.append(eventid_tags) if eventid_tags else None
    tags_list.append(itemid_tags) if itemid_tags else None
    tags_list.append(triggerid_tags) if triggerid_tags else None
    tags_list.append(actionid_tags) if actionid_tags else None
    tags_list.append(hostid_tags) if hostid_tags else None


    trigger_url = create_links_list(
        _bool=True if data_zabbix.get('settings_triggerlinks_bool') and body_messages_url_notes else False,
        url=data_zabbix.get('triggerurl'),
        _type=body_messages_url_emoji_notes)

    host_url = create_links_list(
        _bool=True if data_zabbix.get('settings_hostlinks_bool') and body_messages_url_host else False,
        url=zabbix_host_link.format(zabbix_server=zabbix_api_url, host=data_zabbix.get('host')),
        _type=body_messages_url_emoji_host)

    ack_url = create_links_list(
        _bool=True if data_zabbix.get('settings_acklinks_bool') and body_messages_url_ack else False,
        url=zabbix_ack_link.format(zabbix_server=zabbix_api_url, eventid=data_zabbix.get('eventid')),
        _type=body_messages_url_emoji_ack)

    event_url = create_links_list(
        _bool=True if data_zabbix.get('settings_eventlinks_bool') and body_messages_url_event else False,
        url=zabbix_event_link.format(zabbix_server=zabbix_api_url, eventid=data_zabbix.get('eventid'),
                                     triggerid=data_zabbix.get('triggerid')), _type=body_messages_url_emoji_event)

    if data_zabbix['graphs_period'] != 'default':
        graph_period = data_zabbix['graphs_period']
    else:
        graph_period = zabbix_graph_period_default

    url_list = []
    url_list.append(trigger_url) if trigger_url else None
    for item_id in list(set([x for x in data_zabbix.get('itemid').split()])):
        if re.findall(r"\d+", item_id):
            items_link = create_links_list(
                _bool=True if data_zabbix.get('settings_graphlinks_bool') and body_messages_url_graphs else False,
                url=zabbix_graph_link.format(zabbix_server=zabbix_api_url, itemid=item_id,
                                             range_time=data_zabbix['graphs_period']),
                _type=body_messages_url_emoji_graphs
                                           )
            url_list.append(items_link) if items_link else None
    url_list.append(event_url) if event_url else None
    url_list.append(ack_url) if ack_url else None
    url_list.append(host_url) if host_url else None

    graphs_name = body_messages_title.format(
        title=data_zabbix['title'],
        period_time=set_period_day_hour(graph_period))

    if data_zabbix.get('settings_graphs_bool') and zabbix_graph:
        num_items_id = [item_id for item_id in data_zabbix['itemid'].split() if re.findall(r"\d+", item_id)]
        if len(num_items_id) == 1:
            graphs_png = get_chart_png(itemid=num_items_id[0],
                                       graff_name=graphs_name,
                                       period=graph_period)
        else:
            graphs_png_group = []
            #  get the unique itemid
            for item_id in list(set([x for x in data_zabbix.get('itemid').split()])):
                if re.findall(r"\d+", item_id):
                    chart = get_chart_png(itemid=item_id, graff_name=graphs_name,
                                          period=graph_period).get('img')
                    # A chart we could not fetch used to be wrapped as
                    # InputMediaPhoto(None) and take the whole alert down with it.
                    if chart:
                        graphs_png_group.append(InputMediaPhoto(chart))
            graphs_png = graphs_png_group or False
    else:
        graphs_png = False

    subject = html.escape(substitute_status_emoji(args.subject))

    links = body_messages_url_delimiter.join(url_list) if body_messages_url and len(url_list) != 0 else ''

    tags = body_messages_tags_delimiter.join(tags_list) if body_messages_tags and len(tags_list) != 0 else ''

    # A chart makes the text a caption, and a caption is capped far lower.
    sends_photo = bool(graphs_png) and (
        isinstance(graphs_png, list) or bool(graphs_png.get('img')))
    message = render_message(
        subject, data_zabbix['message'], links, tags,
        more_url=zabbix_event_link.format(
            zabbix_server=zabbix_api_url, eventid=data_zabbix.get('eventid'),
            triggerid=data_zabbix.get('triggerid')),
        limit=tg_caption_max_symbol if sends_photo else tg_message_max_symbol)

    quiet = send_quietly(args.subject, spec=args.quiet_hours)
    send_messages(args.username, message, graphs_png, disable_notification=quiet)
    exit(0)


if __name__ == "__main__":
    main()
