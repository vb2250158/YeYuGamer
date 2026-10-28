"""Root's recorded delayed-dialog frames, represented by inert native fixtures."""
import copy
from datetime import datetime,timedelta,timezone
from pathlib import Path
import unittest
from unittest.mock import Mock,patch
from contextlib import nullcontext

from test_hoyoplay_execution import view,FakeSession,GAME
from yeyu_gamer_manager.services.hoyoplay_launcher import HoYoPlayLauncher
from yeyu_gamer_manager.services.hoyoplay_native_execution import NativeContext
from yeyu_gamer_manager.services.game_launcher import GameLaunchError,GameLaunchCancelled
from yeyu_gamer_manager.services.zzz_launcher_preflight import ZzzLauncherPreflight


class Clock:
    value=0.
    def now(self):return self.value
    def sleep(self,seconds):self.value+=seconds


def disabled_dialog(state):
    packet=view(state)
    packet['nodes'][-1]['enabled']=False
    packet['nodes'][-1]['uia']['invokePatternAvailable']=False
    return packet


class SettlingTests(unittest.TestCase):
    def make(self,frames,cancelled=lambda:False):
        session=FakeSession(frames)
        context=NativeContext('00000000-0000-0000-0000-000000000072','fixture',Path('unused'),nullcontext,Mock())
        driver=HoYoPlayLauncher(object(),GAME,Mock(),cancelled,context=context)
        with patch('yeyu_gamer_manager.services.hoyoplay_launcher.NativeHoYoPlaySession',return_value=session):driver.prepare()
        return driver,session

    def actions(self,session):return [fields['action'] for operation,fields in session.calls if operation=='invoke']

    def test_both_owned_old_dialogs_settle_once_and_busy_frame_reaches_gate(self):
        old=disabled_dialog('repair-confirm')
        old['nodes'][3].update(name='0 等待中',enabled=False)
        frames=[view(),view(menu=True),view('check-complete'),disabled_dialog('check-complete'),view(),
                view(menu=True),view('repair-confirm'),old,view('verifying'),view()]
        clock=Clock()
        with patch('yeyu_gamer_manager.services.hoyoplay_launcher.time.monotonic',clock.now), \
             patch('yeyu_gamer_manager.services.hoyoplay_launcher.time.sleep',clock.sleep):
            driver,session=self.make(frames)
            gate=ZzzLauncherPreflight(driver,GAME,cancel_requested=lambda:False,authority_valid=lambda:True,
                deadline_at=datetime.now(timezone.utc)+timedelta(minutes=5),notify=Mock(),clock=clock.now,sleep=clock.sleep)
            with patch.object(driver,'prepare'):result=gate.run()
        self.assertEqual('launcher-verified',result['state'])
        self.assertEqual(['check','ack-check','verify','confirm-repair'],self.actions(session))
        self.assertEqual([],session.frames)

    def test_persistent_owned_dialog_times_out_without_second_ack(self):
        frames=[view(),view(menu=True),view('check-complete')]+[view('check-complete') for _ in range(50)]
        clock=Clock()
        with patch('yeyu_gamer_manager.services.hoyoplay_launcher.time.monotonic',clock.now), \
             patch('yeyu_gamer_manager.services.hoyoplay_launcher.time.sleep',clock.sleep):
            driver,session=self.make(frames);driver.invoke('check',driver.observe())
            with self.assertRaises(GameLaunchError) as error:driver.observe()
        self.assertEqual('zzz_launcher_dialog_settling_timeout',error.exception.reason_code)
        self.assertEqual(['check','ack-check'],self.actions(session))
        self.assertGreaterEqual(clock.value,8)
        self.assertLess(clock.value,8.3)

    def test_unowned_replacement_dialog_is_not_confirmed(self):
        replacement=view('check-complete');replacement['nodes'][-3]['uia']['runtimeId']=[42,71,999]
        driver,session=self.make([view(),view(menu=True),view('check-complete'),replacement])
        driver.invoke('check',driver.observe())
        with self.assertRaises(GameLaunchError) as error:driver.observe()
        self.assertEqual('zzz_launcher_dialog_settling_unowned_state',error.exception.reason_code)
        self.assertEqual(['check','ack-check'],self.actions(session))

    def test_identity_change_during_settling_rejects(self):
        changed=view();changed['process']['createdAt']+=1
        driver,session=self.make([view(),view(menu=True),view('check-complete'),changed])
        driver.invoke('check',driver.observe())
        with self.assertRaises(GameLaunchError) as error:driver.observe()
        self.assertEqual('zzz_launcher_dialog_settling_identity_changed',error.exception.reason_code)
        self.assertEqual(['check','ack-check'],self.actions(session))

    def test_stop_during_settling_never_reissues_action(self):
        stop=[False]
        driver,session=self.make([view(),view(menu=True),view('check-complete'),view('check-complete')],lambda:stop[0])
        driver.invoke('check',driver.observe())
        with patch('yeyu_gamer_manager.services.hoyoplay_launcher.time.sleep',side_effect=lambda _:stop.__setitem__(0,True)):
            with self.assertRaises(GameLaunchCancelled):driver.observe()
        self.assertEqual(['check','ack-check'],self.actions(session))

    def test_lost_ack_receipt_never_enters_settling_or_retries(self):
        driver,session=self.make([view(),view(menu=True),view('check-complete')])
        driver.invoke('check',driver.observe());request=session.request
        def lost(operation,**fields):
            if operation=='invoke' and fields['action']=='ack-check':raise OSError('lost return')
            return request(operation,**fields)
        with patch.object(session,'request',side_effect=lost) as calls:
            with self.assertRaises(OSError):driver.observe()
            with self.assertRaises(GameLaunchError):driver.observe()
        self.assertEqual(2,calls.call_count)

    def test_ready_after_confirm_cannot_replace_genuine_busy_phase(self):
        driver,session=self.make([view(),view(menu=True),view('repair-confirm'),view()])
        driver.invoke('verify',driver.observe())
        snapshot=driver.observe()
        self.assertEqual('ready',snapshot.state)
        self.assertIsNone(snapshot.completion_receipt)
        self.assertEqual(['verify','confirm-repair'],self.actions(session))

    def test_stale_buffer_is_not_retimestamped(self):
        clock=Clock()
        with patch('yeyu_gamer_manager.services.hoyoplay_launcher.time.monotonic',clock.now):
            driver,session=self.make([view(),view(menu=True),view('repair-confirm'),view('verifying')])
            driver.invoke('verify',driver.observe());clock.value+=2.1
            with self.assertRaises(GameLaunchError) as error:driver.observe()
        self.assertEqual('zzz_launcher_stale_observation',error.exception.reason_code)

    def test_preexisting_repair_dialog_is_never_owned(self):
        driver,session=self.make([view('repair-confirm')])
        with self.assertRaises(GameLaunchError):driver.observe()
        self.assertEqual([],self.actions(session))

if __name__=='__main__':unittest.main()
