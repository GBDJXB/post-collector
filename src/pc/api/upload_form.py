"""鉴权之后解析上传表单，读取实际字节时执行限额。"""

from fastapi import HTTPException, Request
from starlette.datastructures import UploadFile
from starlette.formparsers import MultiPartException, MultiPartParser

from pc.adapters import upload_files


class TooLarge(MultiPartException):
    pass


class BoundedParser(MultiPartParser):
    def __init__(self, *args, media_limit, **kwargs):
        super().__init__(*args, **kwargs)
        self.media_limit = media_limit
        self.file_bytes = 0
        self.total_bytes = 0
        self.header_bytes = 0

    def on_part_begin(self):
        super().on_part_begin()
        self.file_bytes = 0
        self.header_bytes = 0

    def on_header_field(self, data, start, end):
        self.header_bytes += end - start
        if self.header_bytes > 16384:
            raise MultiPartException("上传字段头过长")
        super().on_header_field(data, start, end)

    def on_header_value(self, data, start, end):
        self.header_bytes += end - start
        if self.header_bytes > 16384:
            raise MultiPartException("上传字段头过长")
        super().on_header_value(data, start, end)

    def on_part_data(self, data, start, end):
        if self._current_part.file is not None:
            self.file_bytes += end - start
            self.total_bytes += end - start
            if self.file_bytes > self.media_limit or self.total_bytes > upload_files.MAX_TOTAL_BYTES:
                raise TooLarge("图片大小超过上传限额")
        super().on_part_data(data, start, end)


async def parse(request: Request, *, media_limit: int):
    if not request.headers.get("content-type", "").startswith("multipart/form-data;"):
        raise HTTPException(415, "上传需要 multipart/form-data 表单")
    declared = request.headers.get("content-length")
    if declared is not None:
        try:
            size = int(declared)
        except ValueError as exc:
            raise HTTPException(400, "请求长度无效") from exc
        if size < 0:
            raise HTTPException(400, "请求长度无效")
        if size > upload_files.MAX_REQUEST_BYTES:
            raise HTTPException(413, "上传请求超过 258 MiB")

    async def bounded_stream():
        total = 0
        async for chunk in request.stream():
            total += len(chunk)
            if total > upload_files.MAX_REQUEST_BYTES:
                raise TooLarge("上传请求超过 258 MiB")
            yield chunk

    try:
        form = await BoundedParser(
            request.headers, bounded_stream(), media_limit=media_limit,
            max_files=upload_files.MAX_IMAGES, max_fields=2, max_part_size=1024 * 1024,
        ).parse()
    except MultiPartException as exc:
        raise HTTPException(413 if isinstance(exc, TooLarge) else 400, str(exc)) from exc
    try:
        for key, value in form.multi_items():
            if key not in {"text", "note", "images"}:
                raise HTTPException(422, "上传包含未知字段")
            if (key == "images") != isinstance(value, UploadFile):
                raise HTTPException(422, "上传字段类型无效")
        if len(form.getlist("text")) > 1 or len(form.getlist("note")) > 1:
            raise HTTPException(422, "文本与备注各只能提供一次")
        return form
    except BaseException:
        await form.close()
        raise
