import json
import threading
import time
import unittest
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import patch

import main


class LiveGuardTests(unittest.TestCase):
    def setUp(self):
        self.bot = main.MakerBot.__new__(main.MakerBot)
        b = self.bot
        b.market_lock = threading.RLock()
        b.entry_io_lock = threading.RLock()
        b.guard_wake = threading.Event()
        b.wake_event = threading.Event()
        b.stop_event = threading.Event()
        b.live_mark = Decimal('100')
        b.live_book = (Decimal('99.94'), Decimal('100.06'))
        b.mark_received_at = b.book_received_at = time.monotonic()
        b.guard_position = Decimal(0)
        b.guard_entries = {}
        b.cancel_requests = set()
        b.active_entry_ids = set()
        b.pending_entry_cancels = {}
        b.filled_entry_ids = set()
        b.entry_fill_count = 0
        b.stream = None
        b.log = lambda text: None
        self.posts = []
        b.client = SimpleNamespace(_post_signed=self.post)
        self.live = patch.object(main, 'DRY_RUN', False)
        self.live.start()
        self.addCleanup(self.live.stop)

    def post(self, path, payload):
        self.posts.append((path, dict(payload)))
        return {'code': 0}

    def entry(self, side='buy'):
        cid = main.PREFIX + side
        self.bot.guard_entries[cid] = {
            'side': side, 'price': '99.945' if side == 'buy' else '100.055',
            'cl_ord_id': cid, 'reduce_only': False,
        }
        return cid

    def test_unsorted_book_uses_true_best_prices(self):
        self.assertEqual(main.MakerBot.book_top({
            'bids': [['99', '1'], ['99.94', '2'], ['105', '0']],
            'asks': [['101', '1'], ['100.06', '1']],
        }), (Decimal('99.94'), Decimal('100.06')))

    def test_book_approach_cancels_with_unchanged_mark_and_deduplicates(self):
        cid = self.entry()
        self.bot.on_stream_message(json.dumps({
            'channel': 'depth_book', 'symbol': main.SYMBOL,
            'data': {'bids': [['99.94', '1']], 'asks': [['99.95', '1']]},
        }))
        self.bot.protect_entries_once()
        self.bot.protect_entries_once()
        self.assertEqual(self.posts, [('/api/cancel_order', {'cl_ord_id': cid})])
        self.assertIn(cid, self.bot.guard_entries)  # HTTP acceptance is not cancellation.
        self.assertIn(cid, self.bot.cancel_requests)
        self.bot.on_stream_message(json.dumps({
            'channel': 'order', 'data': {'symbol': main.SYMBOL,
            'cl_ord_id': cid, 'status': 'canceled', 'reduce_only': False},
        }))
        self.assertNotIn(cid, self.bot.guard_entries)
        self.assertNotIn(cid, self.bot.cancel_requests)

    def test_stale_feed_pulls_both_entries_and_blocks_new_entry(self):
        self.entry('buy')
        self.entry('sell')
        self.bot.book_received_at -= main.MARKET_DATA_MAX_AGE + 1
        self.bot.protect_entries_once()
        self.assertEqual(len(self.posts), 2)
        self.assertIsNone(self.bot.send('buy', Decimal('.01'), Decimal('99.945')))
        self.assertEqual(len(self.posts), 2)

    def test_safe_quotes_are_left_on_book(self):
        self.entry('buy')
        self.entry('sell')
        self.bot.protect_entries_once()
        self.assertEqual(self.posts, [])

    def test_mark_approach_and_position_pull_entries(self):
        self.entry('buy')
        self.bot.live_mark = Decimal('99.99')
        self.bot.protect_entries_once()
        self.assertEqual(len(self.posts), 1)
        self.entry('sell')
        self.bot.guard_position = Decimal('.01')
        self.bot.protect_entries_once()
        self.assertEqual(len(self.posts), 2)

    def test_guard_can_cancel_while_main_thread_waits_on_account_data(self):
        self.entry()
        self.bot.live_book = (Decimal('99.94'), Decimal('99.95'))
        worker = threading.Thread(target=self.bot.protect_entries_once)
        worker.start()
        worker.join(timeout=1)
        self.assertFalse(worker.is_alive())
        self.assertEqual(len(self.posts), 1)

    def test_cancel_pending_prevents_replacement(self):
        self.bot.cancel_requests.add('pending-cancel')
        self.assertIsNone(self.bot.send('buy', Decimal('.01'), Decimal('99.945')))
        self.assertEqual(self.posts, [])

    def test_confirmed_missing_cancel_resumes_both_quotes(self):
        cid = self.entry()
        b = self.bot
        b.active_entry_ids.add(cid)
        b.cancel_requests.add(cid)
        b.order_state = lambda cl_id: {'cl_ord_id': cl_id, 'status': 'canceled'}
        b.get_position = lambda: (Decimal(0), Decimal(0))
        b.all_open_orders = lambda: []
        b.entry_qty = lambda mark: Decimal('.01')
        b.price_decimals = 3
        with patch.object(main.time, 'sleep', lambda seconds: None):
            b.manage_entry(Decimal('100'), [])
        self.assertEqual([payload['side'] for path, payload in self.posts], ['buy', 'sell'])
        self.assertNotIn(cid, b.cancel_requests)

    def test_late_cancel_without_active_id_resumes_after_verification(self):
        b = self.bot
        cid = main.PREFIX + 'already-settled'
        # Cancellation races with active-ID reconciliation, leaving an orphan.
        b.cancel_requests.add(cid)
        self.assertNotIn(cid, b.active_entry_ids)
        b.order_state = lambda cl_id: {'cl_ord_id': cl_id, 'status': 'canceled'}
        b.get_position = lambda: (Decimal(0), Decimal(0))
        b.all_open_orders = lambda: []
        b.entry_qty = lambda mark: Decimal('.01')
        b.price_decimals = 3
        with patch.object(main.time, 'sleep', lambda seconds: None):
            b.manage_entry(Decimal('100'), [])
        self.assertNotIn(cid, b.cancel_requests)
        self.assertEqual([payload['side'] for path, payload in self.posts],
                         ['buy', 'sell'])

    def test_invalid_book_discards_cached_market(self):
        self.entry()
        self.bot.on_stream_message(json.dumps({
            'channel': 'depth_book', 'symbol': main.SYMBOL,
            'data': {'bids': [['101', '1']], 'asks': [['100', '1']]},
        }))
        self.bot.protect_entries_once()
        self.assertEqual(len(self.posts), 1)

    def test_http_backup_unblocks_missing_live_feed(self):
        b = self.bot
        b.invalidate_market()
        b.client._get = lambda path, params, auth: (
            {'mark_price': '100'} if path.endswith('query_symbol_price') else
            {'bids': [['99.94', '1']], 'asks': [['100.06', '1']]})
        b.refresh_public_snapshot()
        self.assertEqual(b.live_snapshot(),
                         (Decimal('100'), Decimal('99.94'), Decimal('100.06')))
        b.price_decimals = 3
        b.send('buy', Decimal('.01'), Decimal('99.945'))
        self.assertEqual(len(self.posts), 1)

    def test_slow_http_backup_does_not_mark_old_data_fresh(self):
        b = self.bot
        b.invalidate_market()
        b.client._get = lambda path, params, auth: (
            {'mark_price': '100'} if path.endswith('query_symbol_price') else
            {'bids': [['99.94', '1']], 'asks': [['100.06', '1']]})
        with patch.object(main.time, 'monotonic', side_effect=[10, 14]):
            with self.assertRaises(main.QuoteUnavailable):
                b.refresh_public_snapshot()
        with self.assertRaises(main.QuoteUnavailable):
            b.live_snapshot()

    def test_http_backup_preserves_newer_stream_data(self):
        b = self.bot
        def get(path, params, auth):
            if path.endswith('query_symbol_price'):
                b.live_mark = Decimal('101')
                b.live_book = (Decimal('100.94'), Decimal('101.06'))
                b.mark_received_at = b.book_received_at = 11
                return {'mark_price': '100'}
            return {'bids': [['99.94', '1']], 'asks': [['100.06', '1']]}
        b.client._get = get
        with patch.object(main.time, 'monotonic', side_effect=[10, 12]):
            b.refresh_public_snapshot()
        self.assertEqual(b.live_mark, Decimal('101'))
        self.assertEqual(b.live_book, (Decimal('100.94'), Decimal('101.06')))


if __name__ == '__main__':
    unittest.main()
