"""被动导出源工具只读入口保存的实际输入，保持原调用返回和异常。"""
from contextvars import ContextVar
from datetime import datetime
from functools import wraps
from hashlib import sha256
import json
import os
from pathlib import Path
import uuid

_run = ContextVar('endfield_reward_inspection_run', default=None)


def _emit_safely(emit, *values):
    # 观察通道故障不能代替官方返回值或打断游戏任务。
    try:
        emit(*values)
    except Exception:
        pass


def install(task_type, emit):
    if os.environ.get('YEYU_GAMER_ENDFIELD_REWARD_INSPECTION_ONLY') != '1':
        return False
    if getattr(task_type, '_yeyu_reward_status_observer', False):
        return True
    original_run, original_page = task_type.run, task_type.inspect_page

    @wraps(original_page)
    def page(self, name, *args, **kwargs):
        result = original_page(self, name, *args, **kwargs)
        context = _run.get()
        if context is None or result is None:
            return result
        try:
            if name not in {'daily', 'rewards'} or result.get('page') != name:
                return result
            if len(context['nodes']) >= 13:
                return result
            from src.config import config
            folder = (Path(config.get('screenshots_folder') or 'screenshots')/'reward-status').resolve()
            source = Path(result['imagePath']).resolve(strict=True)
            if not source.is_relative_to(folder) or source.is_symlink():
                return result
            content = source.read_bytes()
            if not 0 < len(content) <= 20*1024*1024 or context['bytes']+len(content)>128*1024*1024:
                return result
            from io import BytesIO
            from PIL import Image
            with Image.open(BytesIO(content)) as image:
                if image.format != 'PNG' or image.size != (3840,2160):
                    return result
            index = len(context['nodes'])
            node = 'daily-status' if name == 'daily' else 'reward-status-'+str(index-1)
            if index == 0 and name != 'daily' or index > 0 and name != 'rewards':
                return result
            identifier = uuid.uuid4().hex
            filename = 'endfield-reward-status-'+identifier+'.png'
            stamp = datetime.now().astimezone().isoformat()
            staging = Path(context['stage']).parent
            (staging/filename).write_bytes(content)
            metadata = {'schemaVersion':1,'method':'official-onnxocr-source-input',
                'node':node,'page':name,'recognitionId':identifier,
                'inputImage':filename,'inputImageSha256':sha256(content).hexdigest(),
                'imageSize':[3840,2160],'coordinateSpace':'recognition-input-pixels',
                'frameCaptureTimeKnown':False,'frameCapturedAt':None,'observedAt':stamp,
                'localIdentityInspection':True,'readonlyInspection':True,
                'sourceTask':'RewardStatusInspectionTask.inspect_page'}
            metadata_name = filename[:-4]+'.json'
            (staging/metadata_name).write_text(json.dumps(metadata,ensure_ascii=False),encoding='utf-8')
            for file,kind in ((filename,'game-ui-recognition-raw'),(metadata_name,'official-recognition-metadata')):
                _emit_safely(emit,context['stage'],'claim-daily-reward','step_artifact',json.dumps({
                    'file':file,'kind':kind,'capturedAt':stamp},ensure_ascii=False))
            context['nodes'].append(node)
            context['bytes'] += len(content)
        except Exception:
            # 观察失败不改变官方返回；缺图由 Manager 保留为未通过验收。
            pass
        return result

    @wraps(original_run)
    def run(self, *args, **kwargs):
        stage = os.environ.get('YEYU_GAMER_STAGE_FILE')
        try:
            selected = json.loads(os.environ.get('YEYU_GAMER_SELECTED_OPERATIONS','[]'))
        except Exception:
            selected = []
        if not isinstance(selected, list) or not all(isinstance(item, str) for item in selected):
            selected = []
        if not stage or 'claim-daily-reward' not in selected:
            return original_run(self,*args,**kwargs)
        token = _run.set({'stage':stage,'nodes':[],'bytes':0})
        _emit_safely(emit,stage,'claim-daily-reward','started','official RewardStatusInspectionTask.run entered')
        try:
            result = original_run(self,*args,**kwargs)
        except Exception as error:
            _emit_safely(emit,stage,'claim-daily-reward','failed','official RewardStatusInspectionTask.run raised '+type(error).__name__)
            raise
        else:
            state = 'failed' if result is False else 'review_required' if result is None else 'completed'
            _emit_safely(emit,stage,'claim-daily-reward',state,
                 'official RewardStatusInspectionTask.run returned; readonly page observations only')
            return result
        finally:
            _run.reset(token)

    task_type.inspect_page,task_type.run = page,run
    task_type._yeyu_reward_status_observer = True
    return True
