import asyncio
from datetime import datetime
from collections import deque
from decimal import Decimal

try:
    import ujson as json
except ImportError:
    import json

import logging
from logging.handlers import RotatingFileHandler
from pprint import pformat
import random
from socket import gaierror
import sys
import time

from dateutil.parser import parse
from dateutil.tz import tzlocal
import requests
import websockets

from trading.exchange import exchange_api_url, exchange_auth
from orderbook.tree import Tree

command_line = False


class Book(object):
    def __init__(self):
        self.matches = deque(maxlen=100)
        self.bids = Tree()
        self.asks = Tree()

        self.level3_sequence = 0
        self.first_sequence = 0
        self.last_sequence = 0
        self.last_time = datetime.now(tzlocal())

    def get_level3(self):
        level_3 = requests.get('http://api.exchange.coinbase.com/products/BTC-USD/book', params={'level': 3}).json()
        [self.bids.insert_order(bid[2], Decimal(bid[1]), Decimal(bid[0]), initial=True) for bid in level_3['bids']]
        [self.asks.insert_order(ask[2], Decimal(ask[1]), Decimal(ask[0]), initial=True) for ask in level_3['asks']]
        self.level3_sequence = level_3['sequence']


class OpenOrders(object):
    def __init__(self):
        self.open_bid_order_id = None
        self.open_bid_price = None

        self.open_ask_order_id = None
        self.open_ask_price = None

        self.insufficient_btc = False
        self.insufficient_usd = False

        self.open_ask_rejections = 0.0
        self.open_bid_rejections = 0.0

    def cancel_all(self):
        if self.open_bid_order_id:
            self.cancel('bid')
        if self.open_ask_order_id:
            self.cancel('ask')

    def cancel(self, side):
        if side == 'bid':
            order_id = self.open_bid_order_id
            price = self.open_bid_price
            self.open_bid_order_id = None
            self.open_bid_price = None
        elif side == 'ask':
            order_id = self.open_ask_order_id
            price = self.open_ask_price
            self.open_ask_order_id = None
            self.open_ask_price = None
        else:
            return False
        response = requests.delete(exchange_api_url + 'orders/' + str(order_id), auth=exchange_auth)
        if response.status_code == 200:
            file_logger.info('canceled {0} {1} @ {2}'.format(side, order_id, price))
        elif 'message' in response.json() and response.json()['message'] == 'order not found':
            file_logger.info('{0} already canceled: {1} @ {2}'.format(side, order_id, price))
        elif 'message' in response.json() and response.json()['message'] == 'Order already done':
            file_logger.info('{0} already filled: {1} @ {2}'.format(side, order_id, price))
        else:
            file_logger.error('Unhandled response: {0}'.format((pformat(response.json()))))
            raise Exception()

    def get_open_orders(self):
        open_orders = requests.get(exchange_api_url + 'orders', auth=exchange_auth).json()

        try:
            self.open_bid_order_id = [order['id'] for order in open_orders if order['side'] == 'buy'][0]
            self.open_bid_price = [order['price'] for order in open_orders if order['side'] == 'buy'][0]
        except IndexError:
            pass

        try:
            self.open_ask_order_id = [order['id'] for order in open_orders if order['side'] == 'sell'][0]
            self.open_ask_price = [order['price'] for order in open_orders if order['side'] == 'sell'][0]
        except IndexError:
            pass

    @property
    def float_open_bid_price(self):
        if self.open_bid_price:
            return float(self.open_bid_price)
        else:
            return 0.0

    @property
    def float_open_ask_price(self):
        if self.open_ask_price:
            return float(self.open_ask_price)
        else:
            return 0.0


class Spreads(object):
    def __init__(self):
        # amount over the highest ask that you are willing to buy btc for
        self.bid_spread = 0.10

        # amount below the lowest bid that you are willing to sell btc for
        self.ask_spread = 0.10

    # spread at which your ask is cancelled
    @property
    def ask_adjustment_spread(self):
        return Decimal(self.ask_spread) + Decimal(0.08)

    # spread at which your bid is cancelled
    @property
    def bid_adjustment_spread(self):
        return Decimal(self.bid_spread) + Decimal(0.08)


file_handler = RotatingFileHandler('log.csv', 'a', 10 * 1024 * 1024, 100)
file_handler.setFormatter(logging.Formatter('%(asctime)s, %(levelname)s, %(message)s'))
file_handler.setLevel(logging.INFO)

file_logger = logging.getLogger('file_log')
file_logger.addHandler(file_handler)
file_logger.setLevel(logging.INFO)

order_book = Book()
open_orders = OpenOrders()
open_orders.cancel_all()
spreads = Spreads()


@asyncio.coroutine
def websocket_to_order_book():
    try:
        websocket = yield from websockets.connect("wss://ws-feed.exchange.coinbase.com")
    except gaierror:
        file_logger.error('socket.gaierror - had a problem connecting to Coinbase feed')
        return
    yield from websocket.send('{"type": "subscribe", "product_id": "BTC-USD"}')

    messages = []
    while True:
        message = yield from websocket.recv()
        messages += [message]
        print(len(messages))
        if len(messages) > 50:
            break

    order_book.get_level3()
    open_orders.get_open_orders()

    [process_message(message) for message in messages]

    while True:
        message = yield from websocket.recv()
        if not process_message(message):
            print(pformat(message))
            raise Exception()
        manage_orders()


def process_message(message):
    if message is None:
        file_logger.error('Websocket message is None.')
        return False

    try:
        message = json.loads(message)
    except TypeError:
        file_logger.error('JSON did not load, see ' + str(message))
        return False

    new_sequence = int(message['sequence'])

    if new_sequence <= order_book.level3_sequence:
        return True

    if not order_book.first_sequence:
        order_book.first_sequence = new_sequence
        order_book.last_sequence = new_sequence
        file_logger.info('Gap between level 3 and first message: {0}'
                         .format(new_sequence - order_book.level3_sequence))
        assert new_sequence - order_book.level3_sequence == 1
    else:
        if (new_sequence - order_book.last_sequence - 1) != 0:
            file_logger.error('sequence gap: {0}'.format(new_sequence - order_book.last_sequence))
            return False
        order_book.last_sequence = new_sequence

    if 'order_type' in message and message['order_type'] == 'market':
        return True

    message_type = message['type']
    message_time = parse(message['time'])
    order_book.last_time = message_time
    side = message['side']

    if message_type == 'received' and side == 'buy':
        order_book.bids.receive(message['order_id'], message['size'])
        return True
    elif message_type == 'received' and side == 'sell':
        order_book.asks.receive(message['order_id'], message['size'])
        return True

    elif message_type == 'open' and side == 'buy':
        order_book.bids.insert_order(message['order_id'], Decimal(message['remaining_size']), Decimal(message['price']))
        return True
    elif message_type == 'open' and side == 'sell':
        order_book.asks.insert_order(message['order_id'], Decimal(message['remaining_size']), Decimal(message['price']))
        return True

    elif message_type == 'match' and side == 'buy':
        order_book.bids.match(message['maker_order_id'], Decimal(message['size']))
        order_book.matches.appendleft((message_time, side, Decimal(message['size']), Decimal(message['price'])))
        return True
    elif message_type == 'match' and side == 'sell':
        order_book.asks.match(message['maker_order_id'], Decimal(message['size']))
        order_book.matches.appendleft((message_time, side, Decimal(message['size']), Decimal(message['price'])))
        return True

    elif message_type == 'done' and side == 'buy':
        order_book.bids.remove_order(message['order_id'])
        if message['order_id'] == open_orders.open_bid_order_id:
            if message['reason'] == 'filled':
                file_logger.info('bid filled @ {0}'.format(open_orders.open_bid_price))
            open_orders.open_bid_order_id = None
            open_orders.open_bid_price = None
            open_orders.insufficient_btc = False
        return True
    elif message_type == 'done' and side == 'sell':
        order_book.asks.remove_order(message['order_id'])
        if message['order_id'] == open_orders.open_ask_order_id:
            if message['reason'] == 'filled':
                file_logger.info('ask filled @ {0}'.format(open_orders.open_ask_price))
            open_orders.open_ask_order_id = None
            open_orders.open_ask_price = None
            open_orders.insufficient_usd = False
        return True

    elif message_type == 'change' and side == 'buy':
        order_book.bids.change(message['order_id'], Decimal(message['new_size']))
        return True
    elif message_type == 'change' and side == 'sell':
        order_book.asks.change(message['order_id'], Decimal(message['new_size']))
        return True

    else:
        file_logger.error('Unhandled message: {0}'.format(pformat(message)))
        raise Exception()


def manage_orders():
        max_bid = Decimal(order_book.bids.price_tree.max_key())
        min_ask = Decimal(order_book.asks.price_tree.min_key())
        if min_ask - max_bid < 0:
            file_logger.warn('Negative spread: {0}'.format(min_ask - max_bid))
            raise Exception()
        if command_line:
            print('Latency: {0:.6f} secs, '
                  'Min ask: {1:.2f}, Max bid: {2:.2f}, Spread: {3:.2f}, '
                  'Your ask: {4:.2f}, Your bid: {5:.2f}, Your spread: {6:.2f}'.format(
                ((datetime.now(tzlocal()) - order_book.last_time).microseconds * 1e-6),
                min_ask, max_bid, min_ask - max_bid,
                open_orders.float_open_ask_price, open_orders.float_open_bid_price,
                                  open_orders.float_open_ask_price - open_orders.float_open_bid_price), end='\r')

        if not open_orders.open_bid_order_id and not open_orders.insufficient_usd:
            if open_orders.insufficient_btc:
                size = 0.1
                open_bid_price = Decimal(round(max_bid + Decimal(open_orders.open_bid_rejections), 2))
            else:
                size = 0.01
                spreads.bid_spread = Decimal(round((random.randrange(15) + 6) / 100, 2))
                open_bid_price = Decimal(round(min_ask - Decimal(spreads.bid_spread)
                                               - Decimal(open_orders.open_bid_rejections), 2))
            order = {'size': size,
                     'price': str(open_bid_price),
                     'side': 'buy',
                     'product_id': 'BTC-USD',
                     'post_only': True}
            response = requests.post(exchange_api_url + 'orders', json=order, auth=exchange_auth)
            if 'status' in response.json() and response.json()['status'] == 'pending':
                open_orders.open_bid_order_id = response.json()['id']
                open_orders.open_bid_price = open_bid_price
                open_orders.open_bid_rejections = 0.0
                file_logger.info('new bid @ {0}'.format(open_bid_price))
            elif 'status' in response.json() and response.json()['status'] == 'rejected':
                open_orders.open_bid_order_id = None
                open_orders.open_bid_price = None
                open_orders.open_bid_rejections += 0.04
                file_logger.warn('rejected: new bid @ {0}'.format(open_bid_price))
            elif 'message' in response.json() and response.json()['message'] == 'Insufficient funds':
                open_orders.insufficient_usd = True
                open_orders.open_bid_order_id = None
                open_orders.open_bid_price = None
                file_logger.warn('Insufficient USD')
            else:
                file_logger.error('Unhandled response: {0}'.format(pformat(response.json())))
                raise Exception()

        if not open_orders.open_ask_order_id and not open_orders.insufficient_btc:
            if open_orders.insufficient_usd:
                size = 0.10
                open_ask_price = Decimal(round(min_ask + Decimal(open_orders.open_ask_rejections), 2))
            else:
                size = 0.01
                spreads.ask_spread = Decimal(round((random.randrange(15) + 6) / 100, 2))
                open_ask_price = Decimal(round(max_bid + Decimal(spreads.ask_spread)
                                               + Decimal(open_orders.open_ask_rejections), 2))
            order = {'size': size,
                     'price': str(open_ask_price),
                     'side': 'sell',
                     'product_id': 'BTC-USD',
                     'post_only': True}
            response = requests.post(exchange_api_url + 'orders', json=order, auth=exchange_auth)
            if 'status' in response.json() and response.json()['status'] == 'pending':
                open_orders.open_ask_order_id = response.json()['id']
                open_orders.open_ask_price = open_ask_price
                file_logger.info('new ask @ {0}'.format(open_ask_price))
                open_orders.open_ask_rejections = 0
            elif 'status' in response.json() and response.json()['status'] == 'rejected':
                open_orders.open_ask_order_id = None
                open_orders.open_ask_price = None
                open_orders.open_ask_rejections += 0.04
                file_logger.warn('rejected: new ask @ {0}'.format(open_ask_price))
            elif 'message' in response.json() and response.json()['message'] == 'Insufficient funds':
                open_orders.insufficient_btc = True
                open_orders.open_ask_order_id = None
                open_orders.open_ask_price = None
                file_logger.warn('Insufficient BTC')
            else:
                file_logger.error('Unhandled response: {0}'.format(pformat(response.json())))
                raise Exception()

        if open_orders.open_bid_order_id and Decimal(open_orders.open_bid_price) < round(
                        min_ask - Decimal(spreads.bid_adjustment_spread), 2):
            open_orders.cancel('bid')

        if open_orders.open_ask_order_id and Decimal(open_orders.open_ask_price) > round(
                        max_bid + Decimal(spreads.ask_adjustment_spread), 2):
            open_orders.cancel('ask')


if __name__ == '__main__':
    if len(sys.argv) == 1:
        stream_handler = logging.StreamHandler()
        stream_handler.setFormatter(logging.Formatter('%(asctime)s, %(levelname)s, %(message)s\n'))
        stream_handler.setLevel(logging.INFO)
        file_logger.addHandler(stream_handler)
        command_line = True

    loop = asyncio.get_event_loop()
    n = 0
    while True:
        start_time = loop.time()
        loop.run_until_complete(websocket_to_order_book())
        end_time = loop.time()
        seconds = end_time - start_time
        if seconds < 2:
            n += 1
            sleep_time = (2 ** n) + (random.randint(0, 1000) / 1000)
            file_logger.error('Websocket connectivity problem, going to sleep for {0}'.format(sleep_time))
            time.sleep(sleep_time)
            if n > 6:
                n = 0
