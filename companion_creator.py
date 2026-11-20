"""Manual boyfriend authoring: validated form -> new, isolated companion pack.

No LLM calls, edits to existing rooms, arbitrary paths, or client-supplied save JSON.
"""
import base64
import binascii
import io
import json
import os
import re
import tempfile
import unicodedata
import uuid
import warnings
from pathlib import Path
from typing import Annotated, Literal
from urllib.parse import urlsplit

from fastapi import HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, StringConstraints, ValidationError, model_validator
from starlette.concurrency import run_in_threadpool

Short = Annotated[str, StringConstraints(strip_whitespace=True, max_length=160)]
Text = Annotated[str, StringConstraints(strip_whitespace=True, max_length=3000)]
Required = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=3000)]
BODY_LIMIT = 3_100_000
IMAGE_LIMIT = 2_000_000
RESERVED = re.compile(r'^(con|prn|aux|nul|com[0-9]|lpt[0-9])$', re.I)


class FormModel(BaseModel):
    model_config = ConfigDict(extra='forbid')


class OpeningPart(FormModel):
    type: Literal['narration', 'dialogue']
    text: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=1800)]


class Metric(FormModel):
    label: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=60)]
    initial: int = Field(default=0, ge=0, le=100, strict=True)


class Milestone(FormModel):
    at: int = Field(ge=0, le=100, strict=True)
    title: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=100)]
    meaning: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=600)]


class BoyfriendCreate(FormModel):
    room_id: Annotated[str, StringConstraints(pattern=r'^[a-z0-9]+(?:-[a-z0-9]+)*$', max_length=64)]
    name: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=100)]
    age: int = Field(ge=18, le=10000, strict=True)
    height_cm: float | None = Field(default=None, gt=0, le=10000)
    weight_kg: float | None = Field(default=None, gt=0, le=100000)
    description: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=500)]
    genre: Short = 'Hiện đại, Lãng mạn'
    origin: Required
    occupation: Required
    residence: Text = ''
    appearance: Required
    personality: Required
    habits: Text = ''
    relationship: Required
    background: Required
    setting: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=1200)]
    starting_situation: Required
    voice: Required
    desire: Required
    flaw: Text = ''
    opening_knowledge: Text = ''
    authority: Text = ''
    initiative: Text = ''
    change_conditions: Text = ''
    ooc_guard: Text = ''
    extra_rules: Text = ''
    opening: list[OpeningPart] = Field(min_length=1, max_length=12)
    mode: Literal['potential_romance', 'friendship', 'rivalry'] = 'potential_romance'
    metrics: list[Metric] = Field(min_length=1, max_length=8)
    title_metric: int = Field(default=0, ge=0, le=7, strict=True)
    milestones: list[Milestone] = Field(min_length=1, max_length=10)
    portrait: Annotated[str, StringConstraints(max_length=2_800_000)] = ''

    @model_validator(mode='after')
    def check_contract(self):
        if RESERVED.fullmatch(self.room_id):
            raise ValueError('Mã nhân vật trùng tên dành riêng của hệ điều hành; hãy đổi mã khác.')
        labels = [m.label.casefold() for m in self.metrics]
        if len(labels) != len(set(labels)):
            raise ValueError('Tên các thuộc tính quan hệ không được trùng nhau.')
        if self.title_metric >= len(self.metrics):
            raise ValueError('Thuộc tính xét danh xưng không tồn tại.')
        levels = [m.at for m in self.milestones]
        titles = [m.title.casefold() for m in self.milestones]
        if levels[0] != 0 or levels != sorted(set(levels)):
            raise ValueError('Mốc danh xưng phải bắt đầu từ 0 và tăng dần, không trùng điểm.')
        if len(titles) != len(set(titles)):
            raise ValueError('Tên các danh xưng không được trùng nhau.')
        authored = self.model_dump(exclude={'portrait'})
        if len(json.dumps(authored, ensure_ascii=False).encode('utf-8')) > 28000:
            raise ValueError('Hồ sơ quá dài (tối đa 28 KB nội dung). Rút gọn để dành context cho cuộc chat.')
        return self


def slug(value):
    normalized = unicodedata.normalize('NFKD', value.lower().replace('đ', 'd'))
    return re.sub(r'[^a-z0-9]+', '-', ''.join(c for c in normalized if not unicodedata.combining(c))).strip('-')


def lines(value):
    return [line.strip() for line in value.splitlines() if line.strip()]


def decode_portrait(data):
    """Verify/decode raster data, remove metadata, and bound stored dimensions."""
    if not data:
        return None
    match = re.fullmatch(r'data:image/(png|jpeg|webp);base64,([A-Za-z0-9+/=\r\n]+)', data)
    if not match:
        raise ValueError('Ảnh phải là PNG, JPEG hoặc WebP; không nhận SVG, đường dẫn hay URL ngoài.')
    try:
        binary = base64.b64decode(match[2], validate=True)
    except (ValueError, binascii.Error) as error:
        raise ValueError('Dữ liệu ảnh không hợp lệ.') from error
    if len(binary) > IMAGE_LIMIT:
        raise ValueError('Ảnh tối đa 2 MB.')
    try:
        from PIL import Image, ImageOps, UnidentifiedImageError
    except ImportError as error:
        raise ValueError('Chưa có thư viện ảnh Pillow. Chạy python -m pip install -r requirements.txt rồi thử lại.') from error
    try:
        with warnings.catch_warnings():
            warnings.simplefilter('error', Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(binary)) as image:
                if image.format not in {'JPEG', 'PNG', 'WEBP'} or image.width * image.height > 16_000_000:
                    raise ValueError('Ảnh tối đa 16 triệu pixel, định dạng PNG/JPEG/WebP.')
                image.load()
                clean = ImageOps.exif_transpose(image).convert('RGB')
                clean.thumbnail((768, 1024))
                clean.info.clear()
                out = io.BytesIO()
                clean.save(out, format='PNG')
                return out.getvalue()
    except (UnidentifiedImageError, OSError, Image.DecompressionBombError, Image.DecompressionBombWarning) as error:
        raise ValueError('Không đọc được ảnh hoặc ảnh vượt giới hạn an toàn.') from error


def build_pack(form, templates, portrait_url):
    """Only shared rules are inherited; no sample identity, lore or future events."""
    bible = json.loads((templates / 'story_bible.json').read_text(encoding='utf-8'))
    state = json.loads((templates / 'room_state.json').read_text(encoding='utf-8'))
    principal = form.room_id.replace('-', '_')
    metrics = {f'metric_{index}': value.label for index, value in enumerate(form.metrics)}
    title_key = f'metric_{form.title_metric}'
    mode = {'title_metric': title_key, 'metrics': metrics,
            'titles': [[m.at, m.title] for m in form.milestones]}
    authority = form.authority or 'Chỉ tự quyết hành động, nguồn lực có căn cứ của mình; không điều khiển Player, không toàn năng.'
    knowledge = form.opening_knowledge or 'Biết quá khứ của mình và điều Player công khai; không đọc suy nghĩ hoặc tự biết bí mật Player.'
    initiative = form.initiative or 'Chủ động bộc lộ ý riêng, ứng tác đời thường đúng tính cách; không ép kết lượt bằng lựa chọn hay câu hỏi bàn giao.'
    change = form.change_conditions or 'Thay đổi qua trải nghiệm và hành vi có hậu quả, không đổi tính hoặc yêu ngay chỉ vì một câu.'
    boundaries = ['Không đọc thought kín, viết thay Player hoặc coi tình cảm là đồng thuận.',
                  'Giữ cá tính và điểm yếu; không toàn năng, không ép kết đôi hoặc tương lai định sẵn.'] + lines(form.ooc_guard)
    character = {'name': form.name, 'age': form.age, 'status': 'alive', 'role': form.occupation,
        'personality': form.personality, 'voice': form.voice, 'desire': form.desire,
        'flaw': form.flaw, 'backstory': form.background, 'opening_knowledge': knowledge,
        'authority': authority, 'initiative': initiative, 'change_conditions': change,
        'everyday_texture': form.habits, 'ooc_guard': boundaries, 'can_loveline': True}
    bible.update(story_id=form.room_id, title=form.name, genre=[g.strip() for g in form.genre.split(',') if g.strip()] or ['Lãng mạn'],
        premise=form.description, tone='Diễn theo personality và voice của nhân vật; chủ động, tự nhiên, tương lai mở.',
        profile_revision='boyfriend-blueprint/1.0-ui',
        profile={'name': form.name, 'age': form.age, 'height_cm': form.height_cm,
            'weight_kg': form.weight_kg, 'origin': form.origin, 'occupation': form.occupation,
            'residence': form.residence, 'appearance': form.appearance, 'personality': form.personality,
            'habits': lines(form.habits), 'relationship_with_player': form.relationship},
        background_before_chat=lines(form.background), starting_situation=form.starting_situation,
        characters={principal: character}, relationship_profiles={principal: {'default_mode': form.mode, form.mode: mode}},
        relationship_title_meanings={principal: {m.title: m.meaning for m in form.milestones}},
        opening_message={'speaker_id': principal, 'sender': form.name, 'is_player': False,
            'can_loveline': True, 'avatar': portrait_url, 'segments': [p.model_dump() for p in form.opening]},
        world={'scope': 'Bối cảnh do hồ sơ này xác định; năng lực và quyền hạn theo authority, không quyền lực vô hạn.',
               'privacy': 'Không nghe thought kín; tự thuật, ý định và sự kiện đã hoàn tất là các loại dữ kiện khác nhau.'})
    bible['strict_rules'] += [
        'Độ dài là mục tiêu sinh câu trọn ý, không cắt đuôi; lời kể và dialogue tách rõ, không nhại lời Player.',
        'Thực hiện [[...]] như chỉ thị đạo diễn trong giới hạn an toàn, canon và agency; NPC không nghe đó như lời nói.',
        'Kế hoạch và lời hứa chưa phải kết quả; giữ ranh giới, xưng hô và tình trạng quan hệ đã xác lập.',
        'Bỏ qua gợi ý thì đổi mạch; khoảng 10 lượt chỉ là điều kiện xem xét lại khi có lý do mới, không lịch nhắc.',
        'Giọng và thói quen là hướng dẫn diễn xuất, không ép lặp câu mẫu hoặc đạo cụ mỗi lượt.'
    ] + lines(form.extra_rules)
    state.update(room_id=form.room_id, story_id=form.room_id, setting=form.setting,
        roster={principal: {'id': principal, 'canon_id': principal, 'name': form.name,
            'role': form.occupation, 'personality': form.personality, 'tone': form.voice,
            'relationship': form.relationship, 'can_loveline': True, 'loveline_hint': '',
            'is_active': True, 'avatar': portrait_url}},
        narrative_state={'relationships': {principal: {'mode': form.mode,
            'metrics': {f'metric_{i}': m.initial for i, m in enumerate(form.metrics)}}}})
    manifest = {'id': form.room_id, 'category': 'boyfriend', 'title': form.name,
        'description': form.description, 'genre': bible['genre'], 'rating': '16+', 'thumbnail': portrait_url}
    return manifest, bible, state


def publish(form, rooms_dir, base_dir):
    """Staging+exclusive per-ID lock. Never overwrite any existing room or image."""
    rooms_dir, base_dir = Path(rooms_dir).resolve(), Path(base_dir).resolve()
    target = rooms_dir / form.room_id
    if target.exists() or target.is_symlink():
        raise FileExistsError('Mã nhân vật đã tồn tại. Chọn mã khác; dữ liệu cũ không bị ghi đè.')
    portrait = decode_portrait(form.portrait)
    staging_root = rooms_dir / '.creator-staging'
    staging_root.mkdir(exist_ok=True)
    if staging_root.is_symlink() or staging_root.resolve().parent != rooms_dir:
        raise ValueError('Thư mục staging không an toàn.')
    lock = staging_root / (form.room_id + '.lock')
    try:
        lock.mkdir()
    except FileExistsError as error:
        raise FileExistsError('Nhân vật này đang được tạo. Chờ rồi kiểm tra chợ trước khi thử lại.') from error
    asset = None
    published = False
    try:
        if portrait:
            proposed = base_dir / 'static' / 'thumbnails' / ('bf-' + uuid.uuid4().hex + '.png')
            if proposed.parent.resolve() != base_dir / 'static' / 'thumbnails':
                raise ValueError('Thư mục ảnh không an toàn.')
            with proposed.open('xb') as stream:
                asset = proposed  # Only clean up an image this request actually created.
                stream.write(portrait)
            portrait_url = '/static/thumbnails/' + asset.name
        else:
            portrait_url = '/static/companion-placeholder.svg'
        manifest, bible, state = build_pack(form, base_dir / 'boyfriend-blueprint' / 'templates', portrait_url)
        # Reuse the same blueprint validator as manual authoring, not a weaker UI-only check.
        import importlib.util
        spec = importlib.util.spec_from_file_location('companion_pack_lint', base_dir / 'boyfriend-blueprint/checks/validate_blueprint.py')
        lint = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(lint)
        lint.validate_pack(manifest, bible, state, base_dir)
        with tempfile.TemporaryDirectory(prefix='bf-', dir=staging_root) as temp:
            folder = Path(temp) / form.room_id
            folder.mkdir()
            for name, data in [('manifest.json', manifest), ('story_bible.json', bible), ('room_state.json', state)]:
                with (folder / name).open('x', encoding='utf-8') as stream:
                    json.dump(data, stream, ensure_ascii=False, indent=2)
            if target.exists() or target.is_symlink():
                raise FileExistsError('Mã nhân vật vừa được dùng. Hãy chọn mã khác.')
            os.rename(folder, target)
            published = True
        return {'id': form.room_id, 'profile_url': '/character/' + form.room_id,
                'market_url': '/boyfriend', 'title': form.name}
    finally:
        if asset is not None and not published:
            asset.unlink(missing_ok=True)
        lock.rmdir()


def check_editor_request(request):
    # Existing app permits CORS *. A custom header alone is not a CSRF boundary.
    if request.headers.get('x-companion-editor') != '1':
        raise HTTPException(403, 'Hãy tạo nhân vật từ form trên website.')
    origin = request.headers.get('origin')
    if origin:
        try:
            source = urlsplit(origin)
        except ValueError as error:
            raise HTTPException(403, 'Nguồn yêu cầu không hợp lệ.') from error
        if source.scheme != request.url.scheme or source.netloc != request.url.netloc:
            raise HTTPException(403, 'Chỉ nhận yêu cầu từ cùng website.')


async def create_from_request(request: Request, rooms_dir: Path, base_dir: Path):
    check_editor_request(request)
    if request.headers.get('content-type', '').split(';')[0].strip() != 'application/json':
        raise HTTPException(415, 'Dữ liệu phải là JSON từ form tạo nhân vật.')
    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > BODY_LIMIT:
            raise HTTPException(413, 'Dữ liệu quá lớn; ảnh tối đa 2 MB, hồ sơ tối đa 28 KB.')
    try:
        form = BoyfriendCreate.model_validate_json(bytes(body))
    except ValidationError as error:
        raise HTTPException(422, detail=error.errors(include_input=False, include_context=False, include_url=False)) from error
    try:
        return await run_in_threadpool(publish, form, rooms_dir, base_dir)
    except FileExistsError as error:
        raise HTTPException(409, str(error)) from error
    except ValueError as error:
        raise HTTPException(422, str(error)) from error
    except OSError as error:
        raise HTTPException(500, 'Không lưu được nhân vật. Kiểm tra quyền ghi/ổ đĩa; không ghi đè dữ liệu cũ.') from error
