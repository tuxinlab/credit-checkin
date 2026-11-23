"""从本机 WorkBuddy 客户端登录态自动解密 accessToken。

机制（参考社区开源项目 88lin/workbuddy-auto-signin，独立实现）：
- 凭据文件 workbuddy-desktop.info 中 accessToken 是 AES-256-GCM 加密 envelope；
- 解密密钥由客户端运行时的 native storage 提供，需借 WorkBuddy 自带的
  Electron 运行时（ELECTRON_RUN_AS_NODE=1）执行一段解密脚本得到；
- 全程 token 只在内存流转，不落盘、不打印。

仅支持 Windows + 已安装并登录 WorkBuddy 桌面端。
"""
from __future__ import annotations

import base64
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, Optional

from .vault import CredentialVaultError


class AutoCredentialError(RuntimeError):
    pass


AUTH_REL = os.path.join("CodeBuddyExtension", "Data", "Public", "auth", "workbuddy-desktop.info")


def auth_file_candidates() -> list:
    override = os.environ.get("WORKBUDDY_AUTH_FILE", "").strip()
    if override:
        return [Path(override)]
    home = Path.home()
    local = os.environ.get("LOCALAPPDATA") or str(home / "AppData" / "Local")
    return [
        Path(local) / AUTH_REL,
        home / ".workbuddy" / "auth" / "workbuddy-desktop.info",
    ]


def find_auth_file() -> Optional[Path]:
    for p in auth_file_candidates():
        if p.is_file():
            return p
    return None


def find_runtime() -> Optional[Path]:
    """探测 WorkBuddy 的 Electron 运行时 exe。"""
    override = os.environ.get("WORKBUDDY_EXE", "").strip()
    if override and Path(override).is_file():
        return Path(override)
    # 先查运行中的进程（最可靠）
    if sys.platform == "win32":
        try:
            import ctypes
            # 用 tasklist 免依赖
            out = subprocess.run(["tasklist", "/FO", "CSV", "/NH"],
                                 capture_output=True, text=True, timeout=10).stdout
            for line in out.splitlines():
                if "workbuddy" in line.lower():
                    # 进程路径需 wmic，改从常见目录找
                    pass
        except Exception:
            pass
    candidates = []
    local = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
    program_files = os.environ.get("ProgramFiles", "C:/Program Files")
    program_files_x86 = os.environ.get("ProgramFiles(x86)", "C:/Program Files (x86)")
    candidates += [
        Path(local) / "Programs" / "WorkBuddy" / "WorkBuddy.exe",
        Path(program_files) / "WorkBuddy" / "WorkBuddy.exe",
        Path(program_files_x86) / "WorkBuddy" / "WorkBuddy.exe",
    ]
    for p in candidates:
        if p.is_file():
            return p
    return None


# 解密脚本：在 WorkBuddy 的 Electron 运行时里执行，输出 {"ok":true,"token":"..."}
_HELPER_JS = r"""
'use strict';
const crypto = require('crypto');
const fs = require('fs');
function fail(reason){ process.stdout.write(JSON.stringify({ok:false,reason:reason})); process.exit(1); }
function obj(x){ return x && typeof x==='object' && !Array.isArray(x); }
function b64(v,len){
  if(typeof v!=='string') fail('INVALID_FORMAT');
  const b=Buffer.from(v,'base64');
  if(b.toString('base64')!==v || (len!==undefined && b.length!==len)) fail('INVALID_FORMAT');
  return b;
}
function decode(value){
  if(!obj(value) || Object.keys(value).sort().join(',')!=='$wbEncrypted,envelope' || value.$wbEncrypted!==1)
    fail('UNSUPPORTED_ENVELOPE');
  let env; try{ env=JSON.parse(Buffer.from(value.envelope,'base64').toString('utf8')); }
  catch(e){ fail('INVALID_FORMAT'); }
  if(!obj(env) || env.suite!==1 || Object.keys(env).sort().join(',')!=='authTag,ciphertext,keyId,nonce,suite')
    fail('UNSUPPORTED_ENVELOPE');
  return {keyId:env.keyId, nonce:b64(env.nonce,12), tag:b64(env.authTag,16), ciphertext:b64(env.ciphertext)};
}
function nativeStorage(){
  try{ const s=process._linkedBinding('electron_browser_workbuddy_storage');
       if(typeof s.loggerGet!=='function') fail('RUNTIME_UNAVAILABLE'); return s; }
  catch(e){ fail('RUNTIME_UNAVAILABLE'); }
}
function runtimeKey(){
  let payload; try{ payload=JSON.parse(nativeStorage().loggerGet()); }
  catch(e){ fail('RUNTIME_UNAVAILABLE'); }
  if(!obj(payload)||payload.version!==1) fail('RUNTIME_UNAVAILABLE');
  return crypto.createHash('sha256').update(payload.atRestSecretKey,'utf8').digest();
}
function aadOf(keyId){
  const lp=s=>{const b=Buffer.from(s,'utf8');const l=Buffer.alloc(4);l.writeUInt32BE(b.length);return Buffer.concat([l,b]);};
  return Buffer.concat([Buffer.from('WB-AAD\0','ascii'),Buffer.from([1]),lp('WBEV1'),lp('sym-v1'),
    Buffer.from([0,0,0,1]),lp(keyId),Buffer.from([2,0,0])]);
}
function openWith(value,key){
  const env=decode(value);
  if(crypto.createHash('sha256').update(key).digest('hex').slice(0,16)!==env.keyId) fail('KEY_MISMATCH');
  let plain;
  try{ const c=crypto.createDecipheriv('aes-256-gcm',key,env.nonce,{authTagLength:16});
       c.setAAD(aadOf(env.keyId)); c.setAuthTag(env.tag);
       plain=Buffer.concat([c.update(env.ciphertext),c.final()]); }
  catch(e){ fail('DECRYPT_FAILED'); }
  return plain;
}
// 昵称/手机号等文本字段：解不出来不影响主流程，返回空串
function softText(value,key){
  try{
    if(!obj(value)||value.$wbEncrypted!==1||typeof value.envelope!=='string') return '';
    const env=JSON.parse(Buffer.from(value.envelope,'base64').toString('utf8'));
    if(!obj(env)||env.suite!==1) return '';
    const nonce=Buffer.from(env.nonce,'base64'),tag=Buffer.from(env.authTag,'base64'),
          ct=Buffer.from(env.ciphertext,'base64');
    const d=crypto.createDecipheriv('aes-256-gcm',key,nonce,{authTagLength:16});
    d.setAAD(aadOf(env.keyId)); d.setAuthTag(tag);
    const text=Buffer.concat([d.update(ct),d.final()]).toString('utf8');
    return text.replace(/[\u0000-\u001f\u007f]/g,'').trim().slice(0,120);
  }catch(e){ return ''; }
}
try{
  const p=__AUTH_PATH__;
  const data=JSON.parse(fs.readFileSync(p,'utf8'));
  const key=runtimeKey();
  const token=openWith(data.auth && data.auth.accessToken, key).toString('utf8');
  if(!token || token.length>32768 || !/^[A-Za-z0-9._~+\/-]+=*$/.test(token)) fail('INVALID_FORMAT');
  const acc=data.account||{};
  const out={ok:true, token:token, uid:(acc.uid)||'',
             domain:(data.auth&&data.auth.domain)||'',
             nickname:softText(acc.nickname,key), phone:softText(acc.phoneNumber,key),
             uin:String(acc.uin||'')};
  process.stdout.write(JSON.stringify(out));
  process.exit(0);
}catch(e){ fail('DECRYPT_FAILED'); }
"""


def auto_detect() -> Dict[str, Any]:
    """自动探测本机 WorkBuddy 凭据，成功返回明文 token（仅内存）。

    返回 {"ok": bool, "token":..., "uid":..., "domain":..., "error":..., "detail":...}
    """
    result = {"ok": False, "token": "", "uid": "", "domain": "",
              "nickname": "", "phone": "", "uin": "", "error": "", "detail": ""}
    if sys.platform != "win32":
        result["error"] = "自动获取目前仅支持 Windows"
        return result

    auth = find_auth_file()
    if not auth:
        result["error"] = "未找到 WorkBuddy 登录态文件，请先在桌面端登录"
        result["detail"] = str(auth_file_candidates()[0])
        return result

    runtime = find_runtime()
    if not runtime:
        result["error"] = "未找到 WorkBuddy 运行时，请确认客户端已安装"
        return result
    result["detail"] = f"运行时: {runtime}"

    try:
        data = json.loads(auth.read_text(encoding="utf-8"))
    except Exception as exc:
        result["error"] = f"读取登录态失败: {type(exc).__name__}"
        return result

    token_obj = (data.get("auth") or {}).get("accessToken")
    if isinstance(token_obj, str):
        # 明文 token（老版本）
        result.update({"ok": True, "token": token_obj.strip(),
                       "uid": str((data.get("account") or {}).get("uid", "")),
                       "domain": str((data.get("auth") or {}).get("domain", ""))})
        return result

    if not isinstance(token_obj, dict) or "$wbEncrypted" not in token_obj:
        result["error"] = "登录态中未找到可用的 accessToken"
        return result

    # 写临时输入文件（只含加密 envelope，不是明文）
    import tempfile
    tmp = Path(tempfile.mkdtemp(prefix="cc-wb-")) / "auth.json"
    tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    env = {k: v for k, v in os.environ.items()
           if not k.upper().startswith(("NODE_", "ELECTRON_", "WORKBUDDY_"))}
    env["ELECTRON_RUN_AS_NODE"] = "1"
    # 把凭据文件路径注入脚本（JSON 字符串转义，避免 argv 在 -e 模式下的歧义）
    script_js = _HELPER_JS.replace("__AUTH_PATH__", json.dumps(str(tmp)))
    try:
        proc = subprocess.run(
            [str(runtime), "-e", script_js],
            capture_output=True, text=True, timeout=20, env=env,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except Exception as exc:
        result["error"] = f"调用运行时失败: {type(exc).__name__}: {exc}"
        return result
    finally:
        try:
            tmp.unlink(missing_ok=True)
            tmp.parent.rmdir()
        except Exception:
            pass

    try:
        reply = json.loads((proc.stdout or "").strip())
    except Exception:
        result["error"] = f"运行时返回无法解析（exit={proc.returncode}）"
        result["detail"] = (proc.stderr or "")[:200]
        return result
    if reply.get("ok"):
        result.update({"ok": True, "token": str(reply.get("token", "")),
                       "uid": str(reply.get("uid", "")),
                       "domain": str(reply.get("domain", "")),
                       "nickname": str(reply.get("nickname", "")),
                       "phone": str(reply.get("phone", "")),
                       "uin": str(reply.get("uin", ""))})
    else:
        reason = str(reply.get("reason", "UNKNOWN"))
        result["error"] = {
            "RUNTIME_UNAVAILABLE": "运行时不支持解密（请确认是官方 WorkBuddy 客户端）",
            "KEY_MISMATCH": "密钥不匹配，请重新登录 WorkBuddy 桌面端",
            "DECRYPT_FAILED": "解密失败，请重新登录 WorkBuddy 桌面端",
        }.get(reason, f"解密失败: {reason}")
    return result
