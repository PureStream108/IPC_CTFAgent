from __future__ import annotations

import base64
import hashlib
import json
from pathlib import Path

DEFAULT_OUTPUT_BYTES = 16_384


def read_page_file(path: Path, *, offset: int = 0, limit: int = 8192) -> dict:
    if offset < 0 or not 1 <= limit <= 8192:
        raise ValueError("invalid artifact page range")
    with path.open("rb") as handle:
        handle.seek(offset)
        raw = handle.read(limit)
    while True:
        try:
            data, encoding = raw.decode("utf-8"), "utf8"
        except UnicodeDecodeError:
            data, encoding = base64.b64encode(raw).decode("ascii"), "base64"
        result = {"path": str(path), "offset": offset, "next_offset": offset + len(raw),
                  "total_bytes": path.stat().st_size, "encoding": encoding, "data": data}
        if len(json.dumps(result, ensure_ascii=False).encode()) <= DEFAULT_OUTPUT_BYTES:
            return result
        raw = raw[:max(1, len(raw) // 2)]


def preview_bytes(value: bytes, limit: int = DEFAULT_OUTPUT_BYTES) -> str:
    limit = max(512, limit)
    if len(value) <= limit:
        return value.decode("utf-8", "replace")
    marker = f"\n[output truncated; {len(value)} bytes total; read the output artifact in pages]\n".encode()
    side = max(1, (limit - len(marker)) // 2)
    return (value[:side].decode("utf-8", "ignore") + marker.decode()
            + value[-side:].decode("utf-8", "ignore"))


def preview_text(value: str, limit: int = DEFAULT_OUTPUT_BYTES) -> str:
    return preview_bytes(value.encode("utf-8"), limit)


def read_output_file(path: Path, limit: int, *, visible_path: str | None = None) -> tuple[str, dict | None]:
    size = path.stat().st_size
    with path.open("rb") as handle:
        if size <= limit:
            return handle.read().decode("utf-8", "replace"), None
        head = handle.read(max(1, (limit - 128) // 2))
        handle.seek(max(0, size - max(1, (limit - 128) // 2)))
        tail = handle.read()
        handle.seek(0)
        digest = hashlib.sha256()
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    marker = f"\n[output truncated; {size} bytes total; read the artifact in pages]\n"
    return head.decode("utf-8", "ignore") + marker + tail.decode("utf-8", "ignore"), {
        "path": visible_path or str(path), "bytes": size, "sha256": digest.hexdigest(),
    }


OUTPUT_FINALIZER = r'''
import hashlib,json,os,sys
limit=max(512,int(sys.argv[4]))
result={"exit_code":int(sys.argv[3]),"stdout":"","stderr":"","artifacts":{}}
for stream,path in zip(("stdout","stderr"),sys.argv[1:3]):
    size=os.path.getsize(path)
    with open(path,"rb") as f:
        if size<=limit:
            result[stream]=f.read().decode("utf-8","replace")
        else:
            side=max(1,(limit-128)//2)
            head=f.read(side);f.seek(max(0,size-side));tail=f.read()
            result[stream]=head.decode("utf-8","ignore")+"\n[output truncated; read the output artifact in pages]\n"+tail.decode("utf-8","ignore")
            digest=hashlib.sha256();f.seek(0)
            while True:
                chunk=f.read(1048576)
                if not chunk:break
                digest.update(chunk)
            result["artifacts"][stream]={"path":path,"bytes":size,"sha256":digest.hexdigest()}
    if stream not in result["artifacts"]:os.unlink(path)
print(json.dumps(result,ensure_ascii=False))
'''

OUTPUT_PAGE_READER = r'''
import base64,json,pathlib,sys
path=pathlib.Path(sys.argv[1]).resolve()
if not path.is_relative_to(pathlib.Path("/workspace")):raise ValueError("artifact path must stay in the workspace")
offset=max(0,int(sys.argv[2]));limit=max(1,min(8192,int(sys.argv[3])))
with path.open("rb") as f:
    f.seek(offset);raw=f.read(limit)
while True:
    try:data,encoding=raw.decode("utf-8"),"utf8"
    except UnicodeDecodeError:data,encoding=base64.b64encode(raw).decode("ascii"),"base64"
    result={"path":str(path),"offset":offset,"next_offset":offset+len(raw),"total_bytes":path.stat().st_size,"encoding":encoding,"data":data}
    if len(json.dumps(result,ensure_ascii=False).encode())<=16000:break
    raw=raw[:max(1,len(raw)//2)]
print(json.dumps(result,ensure_ascii=False))
'''
