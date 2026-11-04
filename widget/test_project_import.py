"""Local manifest validation boundaries without transport or credentials."""
from copy import deepcopy
from pathlib import Path
import sys
import tempfile
import unittest
sys.path.insert(0, str(Path(__file__).resolve().parent))
from project_import import validate_manifest, project_icon, ManifestError

def manifest():
    return {"version": 1, "project": {"name": "测试项目", "icon": {"type": "emoji", "emoji": "🧭"},
        "vision": "将已有素材整理成可使用的目录。", "status": "开发中", "health": "正常",
        "current_goal": "完成真实素材的目录验收。", "next_step": "检查目录入口与分类准确度。", "summary": "索引已生成，等待验收。",
        "progress": {"percent": 65, "evidence": "索引交付已通过，内容验收仍待完成。", "reviewed_at": "2026-10-16T08:00:00+08:00",
            "milestones": [{"name": "生成索引", "weight": 50, "completion": 100, "evidence": "产出索引存在"},
                           {"name": "目录验收", "weight": 50, "completion": 30, "evidence": "抽样验证完成"}]},
        "artifacts": [{"title": "目录索引", "relative_path": "index.md", "result": "生成分组与文件入口。"}],
        "manual_actions": [{"id": "accept-index", "name": "验收目录", "status": "待确认", "decision": "确认分类是否可用",
                            "recommendation": "先检查三个分组", "impact": "决定是否开始正式使用"}],
        "history": [{"id": "index-first", "title": "生成目录索引", "result": "已生成可浏览索引，原完成时间缺少可靠记录。"}]}}


class ManifestTests(unittest.TestCase):
    def test_valid_manifest_keeps_scope_and_real_relative_artifact(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder=Path(tmp); (folder/'index.md').write_text('actual artifact')
            result=validate_manifest(manifest(),folder)['project']
            self.assertEqual(result['progress']['percent'],65)
            self.assertEqual(result['artifacts'][0]['relative_path'],'index.md')
            self.assertEqual(result['manual_actions'][0]['status'],'待确认')
    def test_reject_missing_artifact_and_path_traversal(self):
        with tempfile.TemporaryDirectory() as tmp:
            for path in ('missing.md','../outside.md','/tmp/private.md'):
                value=manifest();value['project']['artifacts'][0]['relative_path']=path
                with self.subTest(path=path),self.assertRaises(ManifestError):validate_manifest(value,Path(tmp))
    def test_unexpected_fields_or_private_credentials_are_rejected(self):
        for field,value in [('unknown','x'),('vision','secret_'+'a'*24),('vision','/Users/owner/private')]:
            m=manifest();m['project'][field]=value
            with self.subTest(field=field),self.assertRaises(ManifestError):validate_manifest(m)
    def test_progress_must_match_weighted_evidence(self):
        m=manifest();m['project']['progress']['percent']=90
        with self.assertRaises(ManifestError):validate_manifest(m)
    def test_bool_and_invalid_revision_are_not_manifest_version(self):
        m=manifest();m['version']=True
        with self.assertRaises(ManifestError):validate_manifest(m)
    def test_https_and_emoji_icons_use_local_validation(self):
        for emoji in ('🧭','👩🏽‍💻','🇨🇳'):
            self.assertEqual(project_icon({'type':'emoji','emoji':emoji})['emoji'],emoji)
        self.assertEqual(project_icon({'type':'external','external':{'url':'https://example.com/icon.png'}})['type'],'external')
        for icon in ({'type':'emoji','emoji':'ABC'},{'type':'external','external':{'url':'http://example.com/a'}}):
            with self.assertRaises(ManifestError):project_icon(icon)
    def test_importing_validator_does_not_load_remote_provider(self):
        import subprocess,json
        code='import sys,json;from project_import import validate_manifest;print(json.dumps([m for m in sys.modules if m.startswith("notion")]))'
        result=subprocess.run([sys.executable,'-c',code],cwd=Path(__file__).parent,capture_output=True,text=True,check=True)
        self.assertEqual(json.loads(result.stdout),[])

if __name__=='__main__':unittest.main()
