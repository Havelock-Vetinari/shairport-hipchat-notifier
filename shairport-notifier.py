#!/usr/bin/env python2.7
# -*- coding: utf-8 -*-


from __future__ import absolute_import
from __future__ import division
from __future__ import print_function
from __future__ import unicode_literals

import base64
import binascii
import hashlib
import json
import logging
import sys
import tempfile
import threading
from collections import defaultdict
from subprocess import call
from time import time, sleep
from ConfigParser import ConfigParser

import requests
import xmltodict


class MetaItem(object):
    CODE = 'code'
    TYPE = 'type'
    DATA = 'data'
    ENCODING = '@encoding'
    TEXT = '#text'
    RAW_DATA = '#raw_data'
    DEASCII_FIELDS = [CODE, TYPE]

    def __init__(self, item):
        super(MetaItem, self).__init__()
        if self.DATA in item:
            data = item[self.DATA]
            if self.ENCODING in data and data[
                self.ENCODING] == 'base64' and self.TEXT in data:
                try:
                    decoded_data = base64.b64decode(data[self.TEXT])
                    try:
                        item[self.DATA][self.RAW_DATA] = decoded_data.decode('utf-8')
                    except Exception as e:
                        logging.error(e)
                        item[self.DATA][self.RAW_DATA] = decoded_data
                except Exception as base_error:
                    logging.error(base_error)

        for field in self.DEASCII_FIELDS:
            if field in item:
                item[field] = binascii.unhexlify(item[field])
        self._raw_item = item

    @property
    def code(self):
        return self._raw_item[self.CODE]

    @property
    def type(self):
        return self._raw_item[self.TYPE]

    @property
    def data(self):
        if self.DATA in self._raw_item and self.RAW_DATA in self._raw_item[self.DATA]:
            return self._raw_item[self.DATA][self.RAW_DATA]
        else:
            return None

    @property
    def raw_data(self):
        return self._raw_item

    def __repr__(self):
        data_len = 0
        if self.data:
            data_len = len(self.data)
        if data_len <= 32:
            return u"<{}:{}/{}: {}>".format(
                self.__class__.__name__, self.type, self.code, self.data)
        else:
            return u"<{}:{}/{}: data length: {}".format(self.__class__.__name__,
                                                        self.type,
                                                        self.code, data_len)


class HipChatNotifier(object):
    MESSAGE_TEMPLATE = u"""
<table><tr><td>
<img src="{art_url}" width="{img_width}"></td><td>
Song: <strong>{song_title}</strong></br>
Artist: <strong>{artist}</strong></br>
Album: <strong>{album}</strong></br>
Genre: <strong>{genre}</strong></td></tr></table>
"""

    def __init__(self, api_token, room, host='api.hipchat.com'):
        self.room = room
        self.host = host
        self.api_token = api_token
        super(HipChatNotifier, self).__init__()

    def send_notification(
            self, song_title=u'Unknown', artist=u'Unknown', album=u'Unknown',
            genre=u'Unknown', art_url=u'', img_width=u'128px', **kwargs
    ):
        url = "https://{0}/v2/room/{1}/notification".format(self.host, self.room)
        headers = {'Content-type': 'application/json'}
        headers['Authorization'] = "Bearer " + self.api_token
        the_message = self.MESSAGE_TEMPLATE.format(
            art_url=art_url,
            img_width=img_width,
            song_title=song_title,
            artist=artist,
            album=album,
            genre=genre,
        )
        payload = {
            'from': 'Now Playing',
            'message': the_message,
            'notify': False,
            'message_format': 'html',
            'color': 'green'
        }
        r = requests.post(url, data=json.dumps(payload), headers=headers)


class MetaDataCollector:
    _data = defaultdict(str)
    _times = defaultdict(int)

    def __init__(self):
        self.flush()

    def __getattr__(self, item):
        return self._data[item]

    def __setattr__(self, key, value):
        self._data[key] = value
        self._times[key] = time()

    def flush(self):
        flushed_data = defaultdict(str, self._data)
        keys = self._data.keys()
        for field in keys:
            self._data[field] = ''
            self._times[field] = 0
        logging.debug(u'{}'.format(flushed_data))
        return flushed_data

    def check_if_arrived(self, fields):
        for field in fields:
            if self._times[field] == 0:
                return False
        return True

    def last_arrive_time(self):
        return self._times[max(self._times)]

    def __repr__(self):
        return u"<{}: [{} {}]>".format(
            self.__class__.__name__,
            self._data, self._times
        )


class SCPUpload:
    REMOTE_DESTINATION = '{user}@{host}:{destination}'
    REMOTE_SOURCE = '{download_path}/{file}.{ext}'
    UPLOAD_COMMAND = 'rsync'
    UPLOAD_COMMAND_OPTIONS = '--chmod=u+rw,g+r,o+r'
    PATH = '{}/{}.{}'

    def __init__(self, host, user, upload_path, download_path):
        self.download_path = download_path
        self.upload_path = upload_path
        self.user = user
        self.host = host

    def upload(self, data):
        art_file = tempfile.NamedTemporaryFile()
        art_file.truncate()
        art_file.write(data)
        art_file.flush()
        file_hash = hashlib.sha256(data).hexdigest()
        destination = self.PATH.format(self.upload_path, file_hash, 'png')
        remote_destination = self.REMOTE_DESTINATION.format(
            user=self.user, host=self.host, destination=destination)
        call([self.UPLOAD_COMMAND, self.UPLOAD_COMMAND_OPTIONS, art_file.name,
              remote_destination])
        art_file.close()
        download_path = self.REMOTE_SOURCE.format(
            download_path=self.download_path, file=file_hash, ext='png'
        )
        return download_path


class App:
    _instance = None

    _collector = None
    _uploader = None
    _notifier = None

    @classmethod
    def get_instance(cls, config_parser):
        if not cls._instance:
            cls._instance = cls(config_parser)
        return cls._instance

    def __init__(self, config_parser):
        self.config_parser = config_parser
        self.config_parser.read('config.ini')
        self._collector = MetaDataCollector()

        self._uploader = SCPUpload(
            **dict(self.config_parser.items('SCPUpload'))
        )
        self._notifier = HipChatNotifier(
            **dict(self.config_parser.items('HipChatNotifier'))
        )

    @property
    def collector(self):
        return self._collector

    def parse_item(self, _, item_dict):
        item = MetaItem(item_dict)
        if item.code == 'PICT':
            art_url = self._uploader.upload(item.data)
            self._collector.art_url = art_url
        elif item.code == 'asaa':
            self._collector.artist = item.data
        elif item.code == 'assa':
            if not self._collector.artist:
                self._collector.artist = item.data
        elif item.code == 'assu':
            self._collector.album = item.data
        elif item.code == 'minm':
            self._collector.song_title = item.data
        elif item.code == 'assn':
            if not self._collector.song_title:
                self._collector.song_title = item.data
        elif item.code == 'asgn':
            self._collector.genre = item.data
        else:
            print(item)
        return True

    def check_for_data_to_send(self):
        while True:
            all_arrived = self._collector.check_if_arrived(
                ['art_url', 'artist', 'album', 'genre', 'song_title']
            )
            last_arrive = self._collector.last_arrive_time()
            if all_arrived:
                if int(last_arrive) > 0 and int(time()) - int(last_arrive) > 5:
                    try:
                        self._notifier.send_notification(
                            **self._collector.flush()
                        )
                    except Exception as error:
                        logging.error(error)
                    finally:
                        sleep(5)
            else:
                if int(last_arrive) > 0 and int(time()) - int(last_arrive) > 10:
                    try:
                        self._notifier.send_notification(
                            **self._collector.flush()
                        )
                    except Exception as error:
                        logging.error(error)
                    finally:
                        sleep(5)
            sleep(2)

    def run(self):
        thread = threading.Thread(target=self.check_for_data_to_send)
        thread.daemon = True
        thread.start()
        while True:
            item_buffer = ''
            while True:
                line = sys.stdin.readline()
                item_buffer += line
                if line.endswith('</item>\n'):
                    try:
                        xmltodict.parse(
                            item_buffer, item_depth=1, item_callback=self.parse_item
                        )
                    except Exception as error:
                        logging.error(error)
                    finally:
                        break


if __name__ == '__main__':
    config_parser = ConfigParser()
    app = App.get_instance(config_parser)
    app.run()
