"""从本机 Trae CN 客户端登录态自动解密 refreshToken / accessToken。

机制（参考社区项目 crush1994/Trae-AutoCheckin，独立实现）：
- storage.json 里 iCubeAuthInfo://icube.cloudide 是 AES-128-CBC 加密值
  （前置 6 字节类型头 + 32 字节随机 + 密文，SHA512 校验）；
- 密钥由固定 salt 派生，算法公开；用 Trae 自带 Electron 运行时的 Node crypto
  解密，避免引入 cryptography 依赖；
- 全程 token 只在内存流转，不落盘、不打印。

仅支持 Windows + 已安装并登录 Trae CN 桌面端。
"""
from __future__ import annotations

import base64
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, Optional

from . import autofill

STORAGE_KEY = "iCubeAuthInfo://icube.cloudide"

# 与社区项目一致的公开 salt（AES key = sha512(sha512(random) xor salt)[0:16]）
SALT_A = bytes([82,9,106,213,48,54,165,56,191,64,163,158,129,243,215,251,
  124,227,57,130,155,47,255,135,52,142,67,68,196,222,233,203,
  84,123,148,50,166,194,35,61,238,76,149,11,66,250,195,78,
  8,46,161,102,40,217,36,178,118,91,162,73,109,139,209,37])
SALT_B = bytes([31,221,168,51,136,7,199,49,177,18,16,89,39,128,236,95,
  96,81,127,169,25,181,74,13,45,229,122,159,147,201,156,239,
  160,224,59,77,174,42,245,176,200,235,187,60,131,83,153,97,
  23,43,4,126,186,119,214,38,225,105,20,99,85,33,12,125])
SALT_C = bytes([191,192,216,250,122,246,220,97,31,254,98,27,8,72,71,176,
  135,99,96,18,127,101,203,104,211,102,191,125,37,72,150,156,
  51,229,121,35,17,153,141,177,110,131,150,128,172,255,254,6,
  18,140,55,62,236,249,135,64,135,12,117,4,89,149,168,209])
SALT_D = bytes([246,204,26,232,232,70,129,109,223,146,169,242,23,241,105,145,
  50,196,165,42,254,120,3,54,244,207,209,85,53,6,138,106,
  175,148,31,204,186,186,165,182,87,142,49,10,39,110,26,154,
  86,56,173,125,18,64,198,225,99,99,83,82,191,134,76,170])
SALT_AES = bytes(a ^ b for a, b in zip(SALT_A, SALT_B))
SALT_AES_PRIVATE = bytes(a ^ b for a, b in zip(SALT_C, SALT_D))


class TraeAutoError(RuntimeError):
    pass


def storage_candidates() -> list:
    env = os.environ.get("TRAE_STORAGE_PATH", "").strip()
    if env:
        return [Path(env)]
    home = Path.home()
    appdata = os.environ.get("APPDATA") or str(home / "AppData" / "Roaming")
    lib = home / "Library" / "Application Support"
    out = []
    for root in (Path(appdata), lib, home, home / ".config"):
        for name in ("Trae CN", "TRAE SOLO CN", "TRAE SOLO", "Trae", ".trae-cn", ".trae"):
            out.append(root / name / "User" / "globalStorage" / "storage.json")
    return out


def find_storage() -> Optional[Path]:
    for p in storage_candidates():
        if p.is_file():
            return p
    return None


def find_runtime() -> Optional[Path]:
    override = os.environ.get("TRAE_EXE", "").strip()
    if override and Path(override).is_file():
        return Path(override)

    # 1) 运行中的进程路径：装在非系统盘也能找到
    for proc_path in autofill.running_process_paths("Trae CN", "Trae"):
        if proc_path.is_file() and "trae" in proc_path.name.lower():
            return proc_path

    program_files = os.environ.get("ProgramFiles", "C:/Program Files")
    program_files_x86 = os.environ.get("ProgramFiles(x86)", "C:/Program Files (x86)")
    candidates = [
        Path(program_files) / "Trae CN" / "Trae CN.exe",
        Path(program_files_x86) / "Trae CN" / "Trae CN.exe",
        Path(program_files) / "Trae" / "Trae.exe",
        Path(program_files_x86) / "Trae" / "Trae.exe",
    ]
    local = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
    candidates.append(Path(local) / "Programs" / "Trae CN" / "Trae CN.exe")
    # 2) 任意盘符的 Program Files
    for root in autofill.drive_roots():
        candidates.append(root / "Program Files" / "Trae CN" / "Trae CN.exe")
        candidates.append(root / "Program Files" / "Trae" / "Trae.exe")

    for p in candidates:
        if p.is_file():
            return p
    return None


# 在 Trae 的 Electron 运行时里执行：Node crypto 解密 AES-128-CBC
_HELPER_JS = r"""
'use strict';
const crypto=require('crypto'), fs=require('fs');
function fail(r){process.stdout.write(JSON.stringify({ok:false,reason:r}));process.exit(1);}
function decFile(){
  const path=__STORAGE_PATH__, saltHex=__SALT_HEX__, type=__TYPE__;
  let data; try{data=JSON.parse(fs.readFileSync(path,'utf8'));}catch(e){fail('BAD_JSON');}
  const enc=data['iCubeAuthInfo://icube.cloudide'];
  if(!enc||typeof enc!=='string') fail('NO_AUTH');
  const buf=Buffer.from(enc,'base64');
  const head=buf.slice(0,6).toString('hex');
  if(head!=='746305100000'&&head!=='123920202003') fail('UNKNOWN_ENC');
  const rnd=buf.slice(6,38), ct=buf.slice(38);
  const salt=Buffer.from(saltHex,'hex');
  const inner=crypto.createHash('sha512').update(rnd).digest();
  const fh=crypto.createHash('sha512').update(Buffer.concat([inner,salt])).digest();
  const key=fh.slice(0,16), iv=fh.slice(16,32);
  let plain;
  try{const d=crypto.createDecipheriv('aes-128-cbc',key,iv);
      plain=Buffer.concat([d.update(ct),d.final()]);}
  catch(e){fail('DECRYPT_FAILED');}
  // 前 64 字节是 sha512 校验，之后是明文，末尾 PKCS7 填充
  const stored=plain.slice(0,64), body=plain.slice(64);
  const text=body.toString('utf8').replace(/\s+$/,'');
  let obj; try{obj=JSON.parse(text);}catch(e){
    // 去掉 PKCS7 填充字节再试
    const trim=text.replace(/[^\x20-\x7e]+$/,'');
    try{obj=JSON.parse(trim);}catch(e2){fail('BAD_PLAINTEXT');}
  }
  let deviceId='';
  for(const k of Object.keys(data)) if(k.indexOf('iCubeAuthInfo://icube-dc:')===0) deviceId=k.split(':').pop();
  process.stdout.write(JSON.stringify({ok:true,auth:obj,deviceId:deviceId}));
  process.exit(0);
}
try{decFile();}catch(e){fail('UNKNOWN');}
"""


def _run_node(runtime: Path, js: str) -> Dict[str, Any]:
    env = {k: v for k, v in os.environ.items()
           if not k.upper().startswith(("NODE_", "ELECTRON_", "TRAE_"))}
    env["ELECTRON_RUN_AS_NODE"] = "1"
    try:
        proc = subprocess.run([str(runtime), "-e", js], capture_output=True, text=True,
                              timeout=20, env=env,
                              creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except Exception as exc:
        return {"ok": False, "reason": f"RUN_FAIL:{type(exc).__name__}"}
    try:
        return json.loads((proc.stdout or "").strip())
    except Exception:
        return {"ok": False, "reason": "BAD_OUTPUT", "stderr": (proc.stderr or "")[:200]}


def auto_detect() -> Dict[str, Any]:
    result = {"ok": False, "token": "", "refresh_token": "", "device_id": "",
              "user_id": "", "account_name": "", "phone": "",
              "error": "", "detail": ""}
    if sys.platform != "win32":
        result["error"] = "自动获取目前仅支持 Windows"
        return result
    storage = find_storage()
    if not storage:
        result["error"] = "未找到 Trae 登录态 storage.json，请先在 Trae CN 客户端登录"
        return result
    runtime = find_runtime()
    if not runtime:
        result["error"] = "未找到 Trae 运行时，请确认客户端已安装"
        return result
    result["detail"] = f"运行时: {runtime}"

    raw = json.loads(storage.read_text(encoding="utf-8"))
    enc = raw.get(STORAGE_KEY)
    if not isinstance(enc, str) or not enc:
        result["error"] = "storage.json 中未找到登录凭据，请先在 Trae CN 客户端登录"
        return result
    # 判断加密类型选 salt
    try:
        head = base64.b64decode(enc)[:6].hex()
    except Exception:
        head = ""
    salt = SALT_AES_PRIVATE if head == "123920202003" else SALT_AES
    js = (_HELPER_JS.replace("__STORAGE_PATH__", json.dumps(str(storage)))
          .replace("__SALT_HEX__", json.dumps(salt.hex()))
          .replace("__TYPE__", json.dumps(head)))
    reply = _run_node(runtime, js)
    if not reply.get("ok"):
        reason = reply.get("reason", "UNKNOWN")
        result["error"] = {
            "NO_AUTH": "未找到登录凭据，请先在 Trae CN 客户端登录",
            "DECRYPT_FAILED": "解密失败，请重新登录 Trae CN 客户端",
            "BAD_PLAINTEXT": "凭据格式变化，请反馈此问题",
            "RUN_FAIL": "调用 Trae 运行时失败",
            "BAD_OUTPUT": "运行时返回异常：" + str(reply.get("stderr", ""))[:100],
        }.get(reason, f"自动获取失败: {reason}")
        return result

    auth = reply.get("auth") or {}
    # auth 里可能是 accessToken / refreshToken 字段
    token = auth.get("accessToken") or auth.get("token") or ""
    refresh = auth.get("refreshToken") or ""
    if not token and not refresh:
        result["error"] = "凭据中未找到 accessToken/refreshToken，请重新登录"
        return result
    acc = auth.get("account") or {}
    result.update({
        "ok": True,
        "token": str(token),
        "refresh_token": str(refresh),
        "device_id": str(reply.get("deviceId", "")),
        "user_id": str(auth.get("userId", "")),
        "account_name": str(acc.get("username") or ""),
        "phone": str(acc.get("nonPlainTextMobile") or ""),
    })
    return result
