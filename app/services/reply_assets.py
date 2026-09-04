"""Private, immutable reply attachments. No model-supplied paths or URLs."""
import hashlib
import base64
import io
import json
import re
import uuid

from PIL import Image, UnidentifiedImageError

MAX_IMAGE_BYTES = 5 * 1024 * 1024
MAX_OWNER_BYTES = 100 * 1024 * 1024
MAX_IMAGES = 4


def initialize_schema(cursor):
    cursor.execute("""CREATE TABLE IF NOT EXISTS reply_assets (
        id TEXT PRIMARY KEY, owner_id INTEGER NOT NULL, mime TEXT NOT NULL,
        width INTEGER NOT NULL, height INTEGER NOT NULL, digest TEXT NOT NULL,
        data BLOB NOT NULL, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        UNIQUE(owner_id, digest), FOREIGN KEY(owner_id) REFERENCES users(id)
    )""")


class ReplyAssets:
    def __init__(self, db):
        self.db = db

    def save(self, owner_id, raw, *, commit=True):
        if not raw or len(raw) > MAX_IMAGE_BYTES:
            raise ValueError("图片不能为空，且不能超过5MB")
        try:
            with Image.open(io.BytesIO(raw)) as picture:
                if picture.format not in {"PNG", "JPEG", "WEBP"} or getattr(picture, "n_frames", 1) != 1:
                    raise ValueError("仅支持静态 PNG、JPEG、WebP 图片")
                width, height = picture.size
                if width * height > 20_000_000 or min(width, height) < 1:
                    raise ValueError("图片尺寸过大，最多2000万像素")
                picture.load()
                # Decode/re-encode to discard metadata and non-image payloads.
                output = io.BytesIO()
                picture.convert("RGBA" if "A" in picture.getbands() else "RGB").save(output, format="PNG")
                data = output.getvalue()
        except (UnidentifiedImageError, OSError, Image.DecompressionBombError) as error:
            raise ValueError("图片无法解码，请更换有效图片") from error
        if len(data) > MAX_IMAGE_BYTES:
            raise ValueError("图片解码后超过5MB，请缩小图片")
        digest = hashlib.sha256(data).hexdigest()
        with self.db.lock:
            row = self.db.conn.execute("SELECT id FROM reply_assets WHERE owner_id=? AND digest=?", (owner_id, digest)).fetchone()
            if row:
                return self.metadata(owner_id, row[0])
            total = self.db.conn.execute("SELECT COALESCE(SUM(length(data)),0) FROM reply_assets WHERE owner_id=?", (owner_id,)).fetchone()[0]
            if total + len(data) > MAX_OWNER_BYTES:
                raise ValueError("回复图片总量不能超过100MB")
            asset_id = uuid.uuid4().hex
            self.db.conn.execute("INSERT INTO reply_assets(id,owner_id,mime,width,height,digest,data) VALUES(?,?,?,?,?,?,?)",
                                 (asset_id, owner_id, "image/png", width, height, digest, data))
            if commit:
                self.db.conn.commit()
            return self.metadata(owner_id, asset_id)

    def export_backup(self, owner_id=None):
        with self.db.lock:
            rows = self.db.conn.execute('SELECT id,owner_id,data FROM reply_assets' +
                (' WHERE owner_id=?' if owner_id is not None else ''), (owner_id,) if owner_id is not None else ()).fetchall()
            return [dict(id=r[0], owner_id=r[1], data=base64.b64encode(r[2]).decode('ascii')) for r in rows]

    def restore_backup(self, entries, owner_id=None):
        mapping = {}
        for entry in entries:
            target = owner_id if owner_id is not None else entry.get('owner_id')
            if not self.db.conn.execute('SELECT 1 FROM users WHERE id=?', (target,)).fetchone():
                raise ValueError('回复图片所属用户不存在')
            encoded = entry.get('data', '')
            if not isinstance(encoded, str) or len(encoded) > MAX_IMAGE_BYTES * 2:
                raise ValueError('图片备份超限')
            raw = base64.b64decode(encoded, validate=True)
            asset = self.save(target, raw, commit=False)
            mapping[(target, entry['id'])] = asset['id']
        return mapping

    def get(self, owner_id, asset_id):
        if not isinstance(asset_id, str) or not re.fullmatch(r"[a-f0-9]{32}", asset_id):
            raise PermissionError("图片不存在或无权限")
        with self.db.lock:
            row = self.db.conn.execute("SELECT id,mime,width,height,digest,data FROM reply_assets WHERE owner_id=? AND id=?", (owner_id, asset_id)).fetchone()
        if not row:
            raise PermissionError("图片不存在或无权限")
        return dict(zip(("id", "mime", "width", "height", "digest", "data"), row))

    def metadata(self, owner_id, asset_id):
        return {k: v for k, v in self.get(owner_id, asset_id).items() if k != "data"}

    def validate(self, owner_id, ids):
        if not isinstance(ids, list) or len(ids) > MAX_IMAGES or len(set(str(i) for i in ids)) != len(ids):
            raise ValueError("每条回复最多4张不同图片")
        for asset_id in ids:
            self.get(owner_id, asset_id)
        return ids


def image_ids(value):
    try:
        result = json.loads(value or "[]") if isinstance(value, str) else value
    except (ValueError, TypeError):
        raise ValueError("无效的图片列表")
    if not isinstance(result, list):
        raise ValueError("无效的图片列表")
    return result
