"""
trading-server is a multi-asset, multi-strategy, event-driven execution
and backtesting platform (OEMS) for trading common markets.

Copyright (C) 2020  Sam Breznikar <sam@sdbgroup.io>

Licensed under GNU General Public License 3.0 or later.

Some rights reserved. See LICENSE.md, AUTHORS.md.
"""

from requests.packages.urllib3.util.retry import Retry
from datetime import timezone, datetime, timedelta
from requests.adapters import HTTPAdapter
from requests import Request, Session
from requests.auth import AuthBase
from urllib.parse import urlparse
from bitmex_ws import Bitmex_WS
from exchange import Exchange
from dateutil import parser
import traceback
import requests
import hashlib
import json
import hmac
import time


class Bitmex(Exchange):
    """
    BitMEX exchange model.
    """

    MAX_BARS_PER_REQUEST = 750
    TIMESTAMP_FORMAT = '%Y-%m-%d%H:%M:%S.%f'
    REQUEST_TIMEOUT = 10

    BASE_URL = "https://www.bitmex.com/api/v1"
    BASE_URL_TESTNET = "https://testnet.bitmex.com/api/v1"
    WS_URL = "wss://www.bitmex.com/realtime"
    BARS_URL = "/trade/bucketed?binSize="
    TICKS_URL = "/trade?symbol="
    POSITIONS_URL = "/position"
    ORDERS_URL = "/order"
    BULK_ORDERS_URL = "/order/bulk"
    TRADE_HIST_URL = "/execution/tradeHistory"

    def __init__(self, logger):
        super()
        self.logger = logger
        self.name = "BitMEX"
        self.symbols = ["XBTUSD"] # "ETHUSD", "XRPUSD", "BCHUSD", "LTCUSD", "LINKUSDT"]

        # Minimum price increment for each instrument.
        self.symbol_min_increment = {
            'XBTUSD': 0.5,
            'ETHUSD': 0.05,
            'XRPUSD': 0.0001,
            'BCHUSD': 0.05,
            'LTCUSD': 0.01,
            'LINKUSDT': 0.0005}

        # Websocket subscription channels.
        self.channels = ["trade"]

        # Not needed but saves a few rest polls/saves time.
        self.origin_tss = {
            "XBTUSD": 1483228800,
            "ETHUSD": 1533200520,
            "XRPUSD": 1580875200,
            'BCHUSD': 1592280000,
            'LTCUSD': 1596081600,
            'LINKUSDT': 1602820800}

        self.api_key, self.api_secret = self.load_api_keys()

        # Connect to websocket stream.
        self.ws = Bitmex_WS(
            self.logger, self.symbols, self.channels, self.WS_URL,
            self.api_key, self.api_secret)
        if not self.ws.ws.sock.connected:
            self.logger.info("Failed to to connect to BitMEX websocket.")

        # Set default https request retry behaviour.
        retries = Retry(
            total=5,
            backoff_factor=0.25,
            status_forcelist=[502, 503, 504],
            method_whitelist=False)
        self.session = Session()
        self.session.mount('https://', HTTPAdapter(max_retries=retries))

        # Non persistent storage for ticks and new 1 min bars.
        self.bars = {}
        self.ticks = {}

        # Note, for future channel subs, create new Bitmex_WS in new process.

    def parse_ticks(self):

        if not self.ws.ws:
            self.logger.info("BitMEX websocket disconnected.")
        else:
            all_ticks = self.ws.get_ticks()
            target_minute = datetime.now().minute - 1
            ticks_target_minute = []
            tcount = 0

            # Search from end of tick list to grab newest ticks first.
            for i in reversed(all_ticks):
                try:
                    ts = i['timestamp']
                    if type(ts) is not datetime:
                        ts = parser.parse(ts)
                except Exception:
                    self.logger.info(traceback.format_exc())

                # Scrape prev minutes ticks.
                if ts.minute == target_minute:
                    ticks_target_minute.append(i)
                    ticks_target_minute[tcount]['timestamp'] = ts
                    tcount += 1

                # Store the previous-to-target bar's last
                # traded price to use as the open price for target bar.
                if ts.minute == target_minute - 1:
                    ticks_target_minute.append(i)
                    ticks_target_minute[tcount]['timestamp'] = ts
                    break

            ticks_target_minute.reverse()

            # Group ticks by symbol.
            self.ticks = {i: [] for i in self.symbols}
            for tick in ticks_target_minute:
                self.ticks[tick['symbol']].append(tick)

            #  Build bars from ticks.
            self.bars = {i: [] for i in self.symbols}
            for symbol in self.symbols:
                bar = self.build_OHLCV(self.ticks[symbol], symbol)
                self.bars[symbol].append(bar)

    def get_bars_in_period(self, symbol, start_time, total):

        if total >= self.MAX_BARS_PER_REQUEST:
            total = self.MAX_BARS_PER_REQUEST

        # Convert epoch timestamp to ISO 8601.
        start = datetime.utcfromtimestamp(start_time).isoformat()
        timeframe = "1m"

        payload = (
            f"{self.BASE_URL}{self.BARS_URL}{timeframe}&"
            f"symbol={symbol}&filter=&count={total}&"
            f"startTime={start}&reverse=false")

        # self.logger.info("API request string: " + payload)

        bars_to_parse = requests.get(payload).json()

        return [
            {
                'symbol': symbol,
                'timestamp': int(parser.parse(bar['timestamp']).timestamp()),
                'open': bar['open'],
                'high': bar['high'],
                'low': bar['low'],
                'close': bar['close'],
                'volume': bar['volume'],
            }
            for bar in bars_to_parse
        ]

    def get_origin_timestamp(self, symbol: str):

        if self.origin_tss[symbol] is not None:
            return self.origin_tss[symbol]
        payload = (
            f"{self.BASE_URL}{self.BARS_URL}1m&symbol={symbol}&filter=&"
            f"count=1&startTime=&reverse=false")

        response = requests.get(payload).json()[0]['timestamp']
        timestamp = int(parser.parse(response).timestamp())

        self.logger.info(f"BitMEX{symbol} origin timestamp: {timestamp}")

        return timestamp

    def get_recent_bars(self, timeframe, symbol, n=1):

        payload = str(
            self.BASE_URL + self.BARS_URL + timeframe +
            "&partial=false&symbol=" + symbol + "&count=" +
            str(n) + "&reverse=true")

        result = requests.get(payload).json()

        return [
            {
                'symbol': symbol,
                'timestamp': i['timestamp'],
                'open': i['open'],
                'high': i['high'],
                'low': i['low'],
                'close': i['close'],
                'volume': i['volume'],
            }
            for i in result
        ]

    def get_recent_ticks(self, symbol, n=1):

        # Find difference between start and end of period.
        delta = n * 60

        # Find start timestamp and convert to ISO1806.
        start_epoch = self.previous_minute() + 60 - delta
        start_iso = datetime.utcfromtimestamp(start_epoch).isoformat()

        # find end timestamp and convert to ISO1806
        end_epoch = self.previous_minute() + 60
        end_iso = datetime.utcfromtimestamp(end_epoch).isoformat()

        # Initial poll.
        time.sleep(1)
        payload = str(
            self.BASE_URL + self.TICKS_URL + symbol + "&count=" +
            "1000&reverse=false&startTime=" + start_iso + "&endTime" + end_iso)

        initial_result = requests.get(payload).json()
        ticks = list(initial_result)
        # If 1000 ticks in result (max size), keep polling until
        # we get a response with length <1000.
        if len(initial_result) == 1000:

            maxed_out = True
            while maxed_out:

                # Dont use endTime as it seems to cut off the final few ticks.
                payload = str(
                    self.BASE_URL + self.TICKS_URL + symbol + "&count=" +
                    "1000&reverse=false&startTime=" + ticks[-1]['timestamp'])

                interim_result = requests.get(payload).json()
                ticks.extend(iter(interim_result))
                if len(interim_result) != 1000:
                    maxed_out = False

        # Check median tick timestamp matches start_iso.
        median_dt = parser.parse(ticks[len(ticks) // 2]['timestamp'])
        match_dt = parser.parse(start_iso)
        if median_dt.minute != match_dt.minute:
            raise Exception("Tick data timestamp error: timestamp mismatch.")

        return [
            i
            for i in ticks
            if parser.parse(i['timestamp']).minute == match_dt.minute
        ]

    def get_position(self, symbol):
        prepared_request = Request(
            'GET',
            self.BASE_URL_TESTNET + self.POSITIONS_URL,
            params='').prepare()
        request = self.generate_request_headers(prepared_request, self.api_key,
                                                self.api_secret)
        response = self.session.send(request).json()

        for pos in response:
            if pos['symbol'] == symbol:

                status = "OPEN" if pos['isOpen'] is True else "CLOSED"
                direction = "LONG" if pos['currentQty'] > 0 else "SHORT"

                return {
                    'size': pos['currentQty'],
                    'avg_entry_price': pos['avgEntryPrice'],
                    'symbol': symbol,
                    'direction': direction,
                    'currency': pos['quoteCurrency'],
                    'opening_timestamp': pos['openingTimestamp'],
                    'opening_size': pos['openingQty'],
                    'status': status}

    def get_executions(self, symbol, start_timestamp=None, end_timestamp=None, count=500):

        # Convert epoch ts's to utc human-readable
        start = str(datetime.utcfromtimestamp(start_timestamp)) if start_timestamp else None
        end = str(datetime.utcfromtimestamp(end_timestamp)) if end_timestamp else None

        payload = {
            'symbol': symbol,
            'count': count,
            'startTime': start,
            'endTime': end,
            'reverse': True}

        prepared_request = Request(
            'GET',
            self.BASE_URL_TESTNET + self.TRADE_HIST_URL,
            json=payload,
            params='').prepare()

        request = self.generate_request_headers(
            prepared_request,
            self.api_key,
            self.api_secret)

        response = self.session.send(request).json()

        executions = []
        for res in response:

            fee_type = "TAKER" if res['lastLiquidityInd'] == "RemovedLiquidity" else "MAKER"
            direction = "LONG" if res['side'] == "Buy" else "SHORT"

            if res['ordStatus'] == "Filled":
                fill = "FILLED"
            elif res['ordStatus'] == "Canceled":
                fill = "CANCELLED"
            elif res['ordStatus'] == "New":
                fill = "NEW"
            elif res['ordStatus'] == "PartiallyFilled":
                fill = "PARTIAL"
            else:
                raise Exception(res['ordStatus'])

            if res['ordType'] == "Limit":
                order_type = "LIMIT"
            elif res['ordType'] == "Market":
                order_type = "MARKET"
            elif res['ordType'] == "StopLimit":
                order_type = "STOP_LIMIT"
            elif res['ordType'] == "Stop":
                order_type = "STOP"
            else:
                raise Exception(res['ordType'])

            executions.append({
                    'order_id': res['clOrdID'],
                    'venue_id': res['orderID'],
                    'timestamp': int(parser.parse(res['timestamp']).timestamp()),
                    'avg_exc_price': res['avgPx'],
                    'currency': res['currency'],
                    'symbol': res['symbol'],
                    'direction': direction,
                    'size': res['lastQty'],
                    'order_type': order_type,
                    'fee_type': fee_type,
                    'fee_amt': res['commission'],
                    'total_fee': res['execComm'] / res['avgPx'],
                    'status': fill})

        return executions

    def close_position(self, symbol, qty=None, direction=None):
        position = self.get_position(symbol)

        if direction == "LONG":
            amt = -qty
        elif direction == "SHORT":
            amt = qty
        else:
            raise Exception(direction)

        if qty and direction:
            payload = {
                'symbol': symbol,
                'orderQty': amt,
                'ordType': "Market"}
        else:
            payload = {
                'symbol': symbol,
                'orderQty': -position['currentQty'],
                'ordType': "Market"}

        # Don't do anything if closing size or position size is 0.
        if payload['orderQty'] != 0 and position['currentQty'] != 0:
            prepared_request = Request(
                'POST',
                self.BASE_URL_TESTNET + self.ORDERS_URL,
                json=payload,
                params='').prepare()

            request = self.generate_request_headers(
                prepared_request,
                self.api_key,
                self.api_secret)

            response = self.session.send(request).json()

            return response['ordStatus'] == "Filled"
        else:
            return False

    def get_orders(self, symbol=None, start_timestamp=None, count=500):

        # Convert epoch ts's to utc human-readable
        start = str(datetime.utcfromtimestamp(start_timestamp)) if start_timestamp else None

        payload = {
            'symbol': symbol,
            'count': count,
            'startTime': start,
            'reverse': True}

        prepared_request = Request(
            'GET',
            self.BASE_URL_TESTNET + self.ORDERS_URL,
            params='', json=payload).prepare()

        request = self.generate_request_headers(prepared_request, self.api_key,
                                                self.api_secret)
        response = self.session.send(request).json()

        orders = []
        for res in response:
            if res['clOrdID']:

                direction = "LONG" if res['side'] == "Buy" else "SHORT"

                if res['ordStatus'] == "Filled":
                    fill = "FILLED"
                elif res['ordStatus'] == "Canceled":
                    fill = "CANCELLED"
                elif res['ordStatus'] == "New":
                    fill = "NEW"
                elif res['ordStatus'] == "PartiallyFilled":
                    fill = "PARTIAL"
                else:
                    raise Exception(res['ordStatus'])

                if res['ordType'] == "Limit":
                    order_type = "LIMIT"
                elif res['ordType'] == "Market":
                    order_type = "MARKET"
                elif res['ordType'] == "StopLimit":
                    order_type = "STOP_LIMIT"
                elif res['ordType'] == "Stop":
                    order_type = "STOP"
                else:
                    raise Exception(res['ordType'])

                # If "\n" in response text field, use substring after "\n".
                if "\n" in res['text']:
                    text = res['text'].split("\n")
                    metatype = text[1]
                elif res['text'] in [
                    "ENTRY",
                    "STOP",
                    "TAKE_PROFIT",
                    "FINAL_TAKE_PROFIT",
                ]:
                    metatype = res['text']
                else:
                    # raise Exception("Order metatype error:", res['text'])
                    metatype = None

                orders.append({
                    'order_id': res['clOrdID'],
                    'venue_id': res['orderID'],
                    'timestamp': int(parser.parse(res['timestamp']).timestamp()),
                    'price': res['price'],
                    'avg_fill_price': res['avgPx'],
                    'currency': res['currency'],
                    'venue': "BitMEX",
                    'symbol': res['symbol'],
                    'direction': direction,
                    'size': res['orderQty'],
                    'order_type': order_type,
                    'metatype': metatype,
                    'void_price': res['stopPx'],
                    'status': fill})

        return orders

    def place_single_order(self, order):

        payload = self.format_orders([order])[0]

        prepared_request = Request(
            'POST',
            self.BASE_URL_TESTNET + self.ORDERS_URL,
            json=payload,
            params='').prepare()

        request = self.generate_request_headers(
            prepared_request,
            self.api_key,
            self.api_secret)

        return self.session.send(request)

    def place_bulk_orders(self, orders):

        # Separate market orders as BitMEX doesnt allow bulk market orders.
        m_o = [o for o in orders if o['order_type'] == "MARKET"]
        nm_o = [o for o in orders if o not in m_o]

        # Send market orders individually amd store responses.
        responses = [self.place_single_order(o) for o in m_o if m_o]

        # Submit non-market orders in a single batch.
        response = None
        if nm_o:
            payload = {'orders': self.format_orders(nm_o)}

            prepared_request = Request(
                'POST',
                self.BASE_URL_TESTNET + self.BULK_ORDERS_URL,
                json=payload,
                params='').prepare()

            request = self.generate_request_headers(
                prepared_request,
                self.api_key,
                self.api_secret)

            response = self.session.send(request)

        # Unpack successful order confirmations and handle errors.
        order_confirmations = []
        for r in responses + [response]:
            if r.status_code == 200:

                res = r.json()
                if isinstance(res, list):
                    order_confirmations.extend(iter(res))
                elif isinstance(res, dict):
                    order_confirmations.append(res)

            elif 401 <= r.status_code <= 404:
                # Syntax, auth or system limit error messages, raise exception.
                # Code likely wrong if this occurs.
                raise Exception(r.status_code, r.json()['error']['message'])

            elif r.status_code == 503:
                # Server overloaded, retry after 500ms, dont raise exception.
                self.logger.info(f"{str(r.status_code)} " + r.json()['error']['message'])
            else:
                self.logger.info(f"{str(r.status_code)} {str(r.json())}")

        updated_orders = []
        if order_confirmations:
            for res in order_confirmations:
                for order in orders:

                    if order['order_id'] == res['clOrdID']:

                        if res['ordStatus'] == "Filled":
                            fill = "FILLED"
                        elif res['ordStatus'] == "Canceled":
                            fill = "CANCELLED"
                        elif res['ordStatus'] == "New":
                            fill = "NEW"
                        elif res['ordStatus'] == "PartiallyFilled":
                            fill = "PARTIAL"
                        else:
                            raise Exception(res['ordStatus'])

                        price = res['stopPx'] if res['stopPx'] else res['price']

                        new = {
                            'trade_id': order['trade_id'],
                            'order_id': order['order_id'],
                            'venue': order['venue'],
                            'symbol': order['symbol'],
                            'order_type': order['order_type'],
                            'metatype': order['metatype'],
                            'void_price': order['void_price'],
                            'direction': order['direction'],
                            'reduce_only': order['reduce_only'],
                            'post_only': order['post_only'],
                            'batch_size': order['batch_size'],
                            'size': order['size'],
                            'trail': order['trail'],
                            'timestamp': int(parser.parse(res['timestamp']).timestamp()),
                            'avg_fill_price': res['avgPx'],
                            'currency': res['currency'],
                            'venue_id': res['orderID'],
                            'price': price,
                            'status': fill}

                        updated_orders.append(new)

        return updated_orders

    def cancel_orders(self, order_ids: list):

        if order_ids[0] is None:
            return None
        payload = {"orderID": order_ids}

        prepared_request = Request(
            "DELETE",
            self.BASE_URL_TESTNET + self.ORDERS_URL,
            json=payload,
            params='').prepare()

        request = self.generate_request_headers(
            prepared_request,
            self.api_key,
            self.api_secret)

        response = self.session.send(request).json()

        response = [response] if not isinstance(response, list) else response

        cancel_confs = {}

        for i in response:

            try:
                price = i['stopPx'] if i['ordType'] == "Stop" else i['price']
            except KeyError:
                print(json.dumps(response, indent=2))
                raise Exception("Unexpected response format: ", i)

            try:
                # Order was filled or cancelled previously.
                if i['error'] is not None:
                    if i['error'] == "Unable to cancel order due to existing state: Filled":
                        cancel_confs[i['orderID']] = {
                            'venue_id': i['orderID'],
                            'order_id': i['clOrdID'],
                            'status': "FILLED",
                            'order_type': i['ordType'],
                            'price': price
                        }

                    elif i['error'] == "Unable to cancel order due to existing state: Canceled":
                        cancel_confs[i['orderID']] = {
                            'venue_id': i['orderID'],
                            'order_id': i['clOrdID'],
                            'status': "CANCELLED",
                            'order_type': i['ordType'],
                            'price': price
                        }

                    else:
                        print(json.dumps(i['error'], indent=2))
                        raise Exception("Unhandled cancellation message case: ", i['error'])

            # Order state unchanged since placement.
            except KeyError:
                if i['ordStatus'] == "Canceled":
                    cancel_confs[i['orderID']] = {
                        'venue_id': i['orderID'],
                        'order_id': i['clOrdID'],
                        'status': "CANCELLED",
                        'order_type': i['ordType'],
                        'price': price
                    }
                else:
                    print(json.dumps(i['ordStatus'], indent=2))
                    raise Exception("Unhandled cancellation message case: ", i['ordStatus'])

        return cancel_confs

    def format_orders(self, orders):

        formatted = []
        # TODO: add logic for execInst and stopPx
        execInst = None
        for order in orders:
            price = self.round_increment(order['price'], order['symbol'])

            stopPx = None
            timeInForce = None

            symbol = order['symbol']
            side = "Buy" if order['direction'] == "LONG" else "Sell"
            orderQty = self.round_increment(order['size'], order['symbol'])
            clOrdID = order['order_id']
            text = order['metatype']

            if order['order_type'] == "LIMIT":
                ordType = "Limit"
                timeInForce = 'GoodTillCancel'

            elif order['order_type'] == "MARKET":
                ordType = "Market"
                price = None
                timeInForce = 'ImmediateOrCancel'

            elif order['order_type'] == "STOP_LIMIT":
                ordType = "StopLimit"
                timeInForce = 'GoodTillCancel'

            elif order['order_type'] == "STOP":
                ordType = "Stop"
                stopPx = price
                price = None
                timeInForce = 'ImmediateOrCancel'

            else:
                raise Exception("Incorrect order type specified.")

            formatted.append({
                    'symbol': symbol,
                    'side': side,
                    'orderQty': orderQty,
                    'price': price,
                    'stopPx': stopPx,
                    'clOrdID': order['order_id'],
                    'ordType': ordType,
                    'timeInForce': timeInForce,
                    'execInst': execInst,
                    'text': text})

        return formatted

    def generate_request_signature(self, secret, request_type, url, nonce,
                                   data):
        """
        Generate BitMEX-compatible authenticated request signature header.

        Args:
            secret: API secret key.
            request_type: Request type (GET, POST, etc).
            url: full request url.
            validity: seconds request will be valid for after creation.
        Returns:
            signature: hex(HMAC_SHA256(apiSecret, verb + path + expires + data)
        Raises:
            None.
        """

        parsed_url = urlparse(url)
        path = parsed_url.path

        if parsed_url.query:
            path = f'{path}?{parsed_url.query}'

        if isinstance(data, (bytes, bytearray)):
            data = data.decode('utf8')

        message = str(request_type).upper() + path + str(nonce) + data
        return hmac.new(
            bytes(secret, 'utf8'), bytes(message, 'utf8'), digestmod=hashlib.sha256
        ).hexdigest()

    def generate_request_headers(self, request, api_key, api_secret):
        """
        Add BitMEX-compatible authentication headers to a request object.

        Args:
            api_key: API key.
            api_secret: API secret key.
            request: Request object to be amended.
        Returns:
            request: Modified request object.
        Raises:
            None.
        """

        nonce = str(int(round(time.time()) + self.REQUEST_TIMEOUT))
        request.headers['api-expires'] = nonce
        request.headers['api-key'] = self.api_key
        request.headers['api-signature'] = self.generate_request_signature(
            self.api_secret, request.method, request.url, nonce, request.body or '')  # noqa
        request.headers['Content-Type'] = 'application/json'
        request.headers['Accept'] = 'application/json'
        request.headers['X-Requested-With'] = 'XMLHttpRequest'

        return request
