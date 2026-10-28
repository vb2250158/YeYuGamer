"""Synthetic update trees; these are NOT measured HoYoPlay update UI samples."""
import copy
from dataclasses import replace
from datetime import datetime,timedelta,timezone
import unittest
from unittest.mock import Mock,patch

from test_hoyoplay_execution import view,GAME
import test_hoyoplay_execution as execution_tests
from test_hoyoplay_settling import Clock
from yeyu_gamer_manager.services.hoyoplay_ui_contract import interpret
from yeyu_gamer_manager.services.game_launcher import GameLaunchError,GameLaunchCancelled
from yeyu_gamer_manager.services.zzz_launcher_preflight import ZzzLauncherPreflight


def available(label='更新'):
    packet=view()
    packet['nodes'].pop(4)
    for index,node in enumerate(packet['nodes']):
        node['index']=index;node['uia']['runtimeId']=[42,71,index]
    packet['nodes'][3]['name']=label
    return packet


def progress(percent=5.,label='模拟阶段甲',*,present=True):
    packet=view('verifying',percent=percent)
    packet['nodes'][4]['name']=label
    packet['nodes'][3]['name']='mutable display number, never parsed'
    packet['gameExecutablePresent']=present
    return packet


class UpdateBranchTests(unittest.TestCase):
    def driver(self,frames):return execution_tests.DriverTests().driver(frames)
    def actions(self,session):return [fields['action'] for operation,fields in session.calls if operation=='invoke']

    def test_download_button_uses_update_action(self):
        packet = available('下载')
        current = interpret(packet)
        self.assertEqual('update-available', current.state)
        self.assertEqual('下载', current.update_target()[0]['name'])
        driver, session = self.driver([packet, copy.deepcopy(packet), progress()])
        driver.invoke('update', driver.observe())
        self.assertEqual('updating', driver.observe().state)
        self.assertEqual(['update'], self.actions(session))

    def test_update_and_download_buttons_together_are_ambiguous(self):
        packet = available()
        duplicate = copy.deepcopy(packet['nodes'][3])
        duplicate['index'] = len(packet['nodes'])
        duplicate['name'] = '下载'
        duplicate['uia']['runtimeId'] = [42, 71, 99]
        packet['nodes'].append(duplicate)
        with self.assertRaises(GameLaunchError):
            interpret(packet)

    def test_official_literal_update_requires_complete_unique_live_scope(self):
        packet=available();current=interpret(packet)
        target,scope=current.update_target()
        self.assertEqual('update-available',current.state)
        self.assertEqual('更新',target['name'])
        self.assertEqual((42,71,2),scope.container_id)
        mutations=[lambda p:p['nodes'][3]['uia'].update(invokePatternAvailable=False),
                   lambda p:p['nodes'][3].update(name='更新游戏'),
                   lambda p:p['nodes'][4].update(name='different checkbox'),
                   lambda p:p['nodes'][5].update(name='unknown named menu'),
                   lambda p:p.update(gameExecutablePresent=False)]
        for mutate in mutations:
            changed=available();mutate(changed)
            with self.subTest(mutate=mutate),self.assertRaises(GameLaunchError):interpret(changed)

    def test_duplicate_update_buttons_never_choose_first(self):
        packet=available();duplicate=copy.deepcopy(packet['nodes'][3]);duplicate['index']=len(packet['nodes'])
        duplicate['uia']['runtimeId']=[42,71,99];packet['nodes'].append(duplicate)
        with self.assertRaises(GameLaunchError):interpret(packet)

    def test_owned_progress_uses_observed_phase_not_download_install_guess(self):
        _,scope=interpret(available()).update_target()
        current=interpret(progress(),update_scope=scope)
        self.assertEqual('updating',current.state)
        self.assertEqual(('official-phase:模拟阶段甲',500),current.progress)
        with self.assertRaises(GameLaunchError):interpret(progress())

    def test_progress_scope_pid_birth_and_document_cannot_rebind(self):
        _,scope=interpret(available()).update_target()
        mutations=[lambda p:p['process'].update(createdAt=999),
                   lambda p:p['nodes'][1]['uia'].update(runtimeId=[42,71,999]),
                   lambda p:p['nodes'][2]['uia'].update(runtimeId=[42,71,998]),
                   lambda p:p['installationAssociation'].update(configSha256='b'*64)]
        for mutate in mutations:
            packet=progress();mutate(packet)
            with self.subTest(mutate=mutate),self.assertRaises(GameLaunchError):interpret(packet,update_scope=scope)

    def test_ambiguous_missing_or_action_labels_do_not_fabricate_progress(self):
        _,scope=interpret(available()).update_target()
        for label in ('1234','下载失败','已暂停','取消更新','Retry','模拟12阶段',''):
            with self.subTest(label=label),self.assertRaises(GameLaunchError):interpret(progress(label=label),update_scope=scope)

    def test_temporary_missing_exe_only_under_owned_update_never_ready(self):
        _,scope=interpret(available()).update_target()
        self.assertEqual('updating',interpret(progress(present=False),update_scope=scope).state)
        packet=view();packet['gameExecutablePresent']=False
        with self.assertRaises(GameLaunchError):interpret(packet,update_scope=scope)
        with self.assertRaises(GameLaunchError):interpret(progress(present=False))

    def test_update_monitor_recheck_repair_ready_complete(self):
        frames=[available(),available(),progress(4),progress(95),progress(2,'模拟阶段乙',present=False),
                progress(100,'模拟阶段乙'),view(),view(menu=True),view('check-complete'),view(),
                view(menu=True),view('repair-confirm'),view('verifying'),view()]
        clock=Clock()
        with patch('yeyu_gamer_manager.services.hoyoplay_launcher.time.monotonic',clock.now), \
             patch('yeyu_gamer_manager.services.hoyoplay_launcher.time.sleep',clock.sleep):
            driver,session=self.driver(frames)
            gate=ZzzLauncherPreflight(driver,GAME,cancel_requested=lambda:False,authority_valid=lambda:True,
                deadline_at=datetime.now(timezone.utc)+timedelta(minutes=5),notify=Mock(),clock=clock.now,sleep=clock.sleep)
            with patch.object(driver,'prepare'):result=gate.run()
        self.assertEqual(dict(state='launcher-verified',updates=1,checks=1,verifications=1),result)
        self.assertEqual(['update','check','ack-check','verify','confirm-repair'],self.actions(session))
        self.assertEqual([],session.frames)

    def test_check_can_discover_update_without_dismissing_unowned_dialog(self):
        driver,session=self.driver([view(),view(menu=True),available(),available(),progress()])
        driver.invoke('check',driver.observe())
        snapshot=driver.observe();self.assertEqual('update-available',snapshot.state)
        driver.invoke('update',snapshot)
        self.assertEqual('updating',driver.observe().state)
        self.assertEqual(['check','update'],self.actions(session))

    def test_process_restart_during_update_never_replays(self):
        changed=progress();changed['process']['createdAt']+=1
        driver,session=self.driver([available(),available(),changed])
        driver.invoke('update',driver.observe())
        with self.assertRaises(GameLaunchError):driver.observe()
        self.assertEqual(['update'],self.actions(session))

    def test_window_loss_during_update_never_relaunches(self):
        driver,session=self.driver([available(),available(),{'waiting':True}])
        driver.invoke('update',driver.observe())
        with self.assertRaises(GameLaunchError):driver.observe()
        self.assertEqual(['update'],self.actions(session))

    def test_second_update_is_never_sent(self):
        driver,session=self.driver([available(),available(),available()])
        driver.invoke('update',driver.observe())
        with self.assertRaises(GameLaunchError):driver.invoke('update',driver.observe())
        self.assertEqual(['update'],self.actions(session))

    def test_lost_update_receipt_poisoned_without_observe_or_retry(self):
        driver,session=self.driver([available(),available()]);snapshot=driver.observe();original=session.request
        def lost(operation,**fields):
            if operation=='invoke':raise OSError('lost update return')
            return original(operation,**fields)
        with patch.object(session,'request',side_effect=lost) as calls:
            with self.assertRaises(OSError):driver.invoke('update',snapshot)
            with self.assertRaises(GameLaunchError):driver.observe()
            with self.assertRaises(GameLaunchError):driver.invoke('update',snapshot)
        self.assertEqual(2,calls.call_count)

    def test_stop_after_update_prevents_observation_or_more_actions(self):
        driver,session=self.driver([available(),available()])
        driver.invoke('update',driver.observe());driver._cancel_requested=lambda:True
        with self.assertRaises(GameLaunchCancelled):driver.observe()
        self.assertEqual(['update'],self.actions(session))

    def test_unbounded_phase_labels_cannot_renew_stall_budget(self):
        clock=Clock();gate=ZzzLauncherPreflight(Mock(),GAME,cancel_requested=lambda:False,authority_valid=lambda:True,
            deadline_at=datetime.now(timezone.utc)+timedelta(minutes=5),notify=Mock(),clock=clock.now)
        from yeyu_gamer_manager.services.hoyoplay_launcher import LauncherSnapshot
        snapshot=LauncherSnapshot('updating',0,71,123,str(GAME),frozenset(),('phase',1))
        for i in range(8):gate.observe_progress(replace(snapshot,progress=(str(i),1)))
        with self.assertRaises(GameLaunchError) as error:gate.observe_progress(replace(snapshot,progress=('extra',1)))
        self.assertEqual('zzz_launcher_progress_phase_limit',error.exception.reason_code)

if __name__=='__main__':unittest.main()
