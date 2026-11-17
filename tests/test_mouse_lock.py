from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from talkturn import mouse_lock, runtime, service
from talkturn.mouse_lock import EscHold


class MouseLockTest(unittest.TestCase):
    def test_tap_is_replayed_but_a_continuous_hold_fires_once(self):
        esc = EscHold(hold=2.5, gap=1.5, tap=0.4)
        esc.down(0.0)
        self.assertTrue(esc.up(0.1))            # ordinary Esc still reaches the app
        esc.down(10.0)
        self.assertFalse(esc.up(11.0))          # aborted hold: neither out nor a stray Esc
        self.assertEqual(esc.poll(11.0), 0.0)
        for step in range(26):                  # typematic repeats every 0.1 s
            esc.down(20.0 + step / 10)
        self.assertLess(esc.poll(22.4), 1.0)
        self.assertFalse(esc.fired)
        self.assertEqual(esc.poll(22.5), 1.0)
        self.assertTrue(esc.fired)
        self.assertFalse(esc.up(22.6))          # the firing press is never replayed

    def test_missed_key_up_cannot_fire_later(self):
        esc = EscHold(hold=2.5, gap=1.5)
        esc.down(0.0)
        self.assertEqual(esc.poll(5.0), 0.0)    # no repeats for longer than the gap
        self.assertFalse(esc.fired)
        self.assertIsNone(esc.since)

    def test_lock_engages_only_for_a_running_receiver_and_never_twice(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(runtime, 'ROOT', Path(folder)), \
             patch.object(mouse_lock.subprocess, 'Popen') as spawn, \
             patch.object(mouse_lock, 'active', return_value=False) as active:
            self.assertFalse(mouse_lock.engage())                    # no receiver pid at all
            (Path(folder) / 'reply-bridge.pid').write_text('4321', encoding='utf-8')
            with patch.object(service, 'process_running', return_value=False):
                self.assertFalse(mouse_lock.engage())                # stale pid
            spawn.assert_not_called()
            with patch.object(service, 'process_running', return_value=True):
                self.assertTrue(mouse_lock.engage())
                self.assertEqual(spawn.call_args.args[0][-3:], ['-m', 'talkturn.mouse_lock', '4321'])
                active.return_value = True
                self.assertTrue(mouse_lock.engage())                 # already locked: no second process
            spawn.assert_called_once()


if __name__ == '__main__':
    unittest.main()
