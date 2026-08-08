from django.template.response import TemplateResponse
from django.contrib import messages
from django.contrib.auth.decorators import login_required, permission_required
from django.conf import settings

import os
import re
import numpy as np
import requests

from astropy.table import Table
from astropy.io import fits
from astropy.time import Time
from astropy.wcs import WCS

from stdpipe import astrometry

from . import models
from . import forms

# SVO/STDWeb 滤光片名（mag_filter_name）→ AJST band 映射表
# 已对照 AJST filters 表定稿（SELECT id FROM filters，81 个 band）：
# 光学 u/g/r/i/z、U/B/V/R/I、J/H/Ks、G 均存在。
# AJST filters 表无 Gaia BP/RP band，故 BP/RP 不映射，原样传递
# （预览标黄提示手工修改；AJST 侧查不到也仅 warning 不阻断）。
AJST_FILTER_MAP = {
    # STDWeb 内部星等列名
    'Umag': 'U',
    'Bmag': 'B',
    'Vmag': 'V',
    'Rmag': 'R',
    'Imag': 'I',
    'umag': 'u',
    'gmag': 'g',
    'rmag': 'r',
    'imag': 'i',
    'zmag': 'z',
    'Jmag': 'J',
    'Hmag': 'H',
    'Ksmag': 'Ks',
    'Gmag': 'G',
    # SVO 滤光片名
    'bessellu': 'U',
    'bessellb': 'B',
    'bessellv': 'V',
    'bessellr': 'R',
    'besselli': 'I',
    'sdssu': 'u',
    'sdssg': 'g',
    'sdssr': 'r',
    'sdssi': 'i',
    'sdssz': 'z',
    '2massj': 'J',
    '2massh': 'H',
    '2massks': 'Ks',
    'gaia::g': 'G',
}

# Fields carried per preview row through POST (row_<i>_<field>)
ROW_FIELDS = [
    'task_id', 'types', 'svo_filter', 'original_name',
    'mjd', 'mag', 'mag_err', 'limiting_mag', 'band',
    'ra', 'dec', 'transient_id', 'mag_system',
    'telescope', 'instrument', 'reference',
]


def ajst_request(path, method='GET', payload=None, params=None, request=None):
    """Unified HTTP wrapper for the AJST ingest API.

    Bearer-token authenticated, 10s timeout. Errors are converted to Django
    messages (when `request` is given) and reported as (status, data).
    """
    url = settings.AJST_BASE_URL.rstrip('/') + path

    if settings.AJST_TOKEN is not None:
        headers = {'Authorization': f'Bearer {settings.AJST_TOKEN}'}
    else:
        headers = None

    try:
        res = requests.request(method, url, headers=headers, params=params, json=payload, timeout=10)
    except requests.RequestException as e:
        if request is not None:
            messages.error(request, f'AJST 请求失败: {e}')
        return None, None

    try:
        data = res.json()
    except ValueError:
        data = None

    if res.status_code >= 400 and request is not None:
        err = (data or {}).get('error') or res.text[:200]
        messages.error(request, f'AJST 返回错误 {res.status_code}: {err}')

    return res.status_code, data


def ajst_resolve(request=None, name=None, ra=None, dec=None, radius=5.0):
    """GET /api/ingest/resolve - returns list of candidates (never creates)."""
    params = {'radius': radius}
    if name:
        params['name'] = name
    if ra is not None:
        params['ra'] = ra
    if dec is not None:
        params['dec'] = dec

    status, data = ajst_request('/api/ingest/resolve', 'GET', params=params, request=request)

    if status == 200 and data:
        return data.get('candidates', [])

    return []


def ajst_upload(payload, request=None):
    """POST /api/ingest/photometry"""
    return ajst_request('/api/ingest/photometry', 'POST', payload=payload, request=request)


def ajst_task_coords(task):
    """Guess coordinates relevant for the task.

    Fallback logic mirrors skyportal_resolve_task: task config target_ra/dec
    first, then the WCS frame center. Returns (ra, dec) or (None, None).
    """
    if 'target_ra' in task.config and 'target_dec' in task.config:
        return task.config.get('target_ra'), task.config.get('target_dec')

    try:
        filename = os.path.join(task.path(), 'image.fits')
        wcsname = os.path.join(task.path(), 'image.wcs')

        header = fits.getheader(filename)
        if os.path.exists(wcsname):
            wcs = WCS(fits.getheader(wcsname))
            astrometry.clear_wcs(header)
            header += wcs.to_header(relax=True)

        ra, dec, sr = astrometry.get_frame_center(header=header)

        return ra, dec
    except Exception:
        return None, None


def _float_or_none(value):
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    if not np.isfinite(v):
        return None
    return v


def parse_ids(ids_str):
    """Parse task id list, supporting `a-b` ranges (mirrors views_skyportal)."""
    ids = [int(_.strip()) for _ in re.split(r'\W+', ids_str or '') if _.isnumeric()]

    if '-' in (ids_str or '') and len(ids) == 2:
        ids = [int(_) for _ in np.arange(ids[0], ids[1]+1)]

    return ids


def build_task_rows(task, types, transient_id=None):
    """Build initial preview rows for a task, one per data type actually
    present on disk (direct: target.vot, subtracted: sub_target.vot)."""
    rows = []

    for t in types:
        sname = 'target.vot' if t == 'direct' else 'sub_target.vot'
        filename = os.path.join(task.path(), sname)
        if not os.path.exists(filename):
            continue

        row = {
            'task_id': task.id,
            'types': t,
            'original_name': task.original_name,
            'selected': True,
            'svo_filter': '',
            'band': '',
            'band_unmapped': False,
            'mag': None,
            'mag_err': None,
            'limiting_mag': None,
            'ra': None,
            'dec': None,
            'transient_id': transient_id or '',
            't0': None,
            'distance': None,
            'mag_system': 'AB',
            'telescope': settings.AJST_DEFAULT_TELESCOPE,
            'instrument': settings.AJST_DEFAULT_INSTRUMENT,
            'reference': f'STDWeb task {task.id}',
        }

        try:
            tobj = Table.read(filename)
            r0 = tobj[0]

            svo = str(r0['mag_filter_name'])
            row['svo_filter'] = svo
            row['band'] = AJST_FILTER_MAP.get(svo, svo)
            row['band_unmapped'] = svo not in AJST_FILTER_MAP

            mag = _float_or_none(r0['mag_calib'])
            mag_err = _float_or_none(r0['mag_calib_err'])

            # Only pass the magnitude when its error is good enough,
            # otherwise upload the upper limit only (same rule as SkyPortal)
            if mag is not None and mag_err is not None and mag_err < 1/3:
                row['mag'] = mag
                row['mag_err'] = mag_err
            else:
                row['limiting_mag'] = _float_or_none(r0['mag_limit'])

            row['ra'] = _float_or_none(r0['ra'])
            row['dec'] = _float_or_none(r0['dec'])
        except Exception as e:
            row['error'] = f'读取测光文件失败: {e}'

        if row['ra'] is None or row['dec'] is None:
            row['ra'], row['dec'] = ajst_task_coords(task)

        row['mjd'] = None
        if task.config.get('time'):
            try:
                row['mjd'] = Time(task.config['time']).mjd
            except Exception:
                pass
        row['mjd_missing'] = row['mjd'] is None

        rows.append(row)

    return rows


def resolve_rows(request, rows):
    """Resolve each row against AJST (by transient_id or by coordinates),
    filling transient_id / t0 / distance. Read-only, never creates sources."""
    for row in rows:
        row['t0'] = None
        row['distance'] = None

        if row.get('error'):
            continue

        tid = (row.get('transient_id') or '').strip()
        ra = _float_or_none(row.get('ra'))
        dec = _float_or_none(row.get('dec'))

        candidates = []
        if tid:
            candidates = ajst_resolve(request, name=tid)
        elif ra is not None and dec is not None:
            candidates = ajst_resolve(request, ra=ra, dec=dec)

        if candidates:
            cand = candidates[0] # Nearest candidate
            row['transient_id'] = cand.get('id') or tid
            row['t0'] = cand.get('t0')
            row['distance'] = cand.get('distance_arcsec')

    return rows


def rows_from_post(post):
    """Reconstruct preview rows purely from submitted form values."""
    rows = []

    for i in post.getlist('row_index'):
        row = {field: (post.get(f'row_{i}_{field}') or '').strip() for field in ROW_FIELDS}
        row['selected'] = post.get(f'row_{i}_selected') is not None
        if str(row['task_id']).isdigit():
            row['task_id'] = int(row['task_id'])
        rows.append(row)

    return rows


def upload_rows(request, rows, create_if_missing=False, new_t0=None):
    """Build the payload strictly from POSTed row values (never re-reads
    .vot files), validate each selected row, and upload per transient."""
    groups = {}

    for row in rows:
        if not row['selected']:
            continue

        row['error'] = None
        row['status'] = None

        mjd = _float_or_none(row['mjd'])
        mag = _float_or_none(row['mag'])
        mag_err = _float_or_none(row['mag_err'])
        limiting_mag = _float_or_none(row['limiting_mag'])
        band = row['band'].strip()
        tid = row['transient_id'].strip()
        ra = _float_or_none(row['ra'])
        dec = _float_or_none(row['dec'])

        # Basic STDWeb-side validation to avoid pointless AJST 400s
        if mjd is None:
            row['error'] = 'MJD 缺失或非法'
            continue
        if mag is None and limiting_mag is None:
            row['error'] = 'mag 与 limiting_mag 至少填写一项'
            continue
        if not band:
            row['error'] = 'band 不能为空'
            continue
        if not tid and (ra is None or dec is None):
            row['error'] = '无 transient_id 且坐标缺失，无法解析源'
            continue

        key = tid or f'{ra:.6f},{dec:.6f}'
        group = groups.setdefault(key, {
            'transient_id': tid,
            'ra': ra,
            'dec': dec,
            'rows': [],
            'points': [],
        })

        group['rows'].append(row)
        group['points'].append({
            'mjd': mjd,
            'mag': mag,
            'mag_err': mag_err if mag is not None else None,
            'limiting_mag': limiting_mag if mag is None else None,
            'mag_system': row['mag_system'] or 'AB',
            'band': band,
            'telescope': row['telescope'] or settings.AJST_DEFAULT_TELESCOPE,
            'instrument': row['instrument'] or settings.AJST_DEFAULT_INSTRUMENT,
            'reference': row['reference'] or f"STDWeb task {row['task_id']}",
            'extra_data': {
                'stdweb_task_id': row['task_id'],
                'stdweb_types': row['types'],
                'svo_filter': row['svo_filter'],
                'stdweb_original_name': row['original_name'],
            },
        })

    for group in groups.values():
        payload = {
            'transient_id': group['transient_id'] or None,
            'ra': group['ra'],
            'dec': group['dec'],
            'resolve_radius': 5.0,
            'create_if_missing': create_if_missing,
            'points': group['points'],
        }

        if create_if_missing:
            payload['new_transient'] = {
                'id': group['transient_id'],
                'ra': group['ra'],
                'dec': group['dec'],
                't0': str(new_t0) if new_t0 else None,
                'aliases': [],
            }

        status, data = ajst_upload(payload, request=request)

        for row in group['rows']:
            if status == 200 and data is not None:
                row['status'] = (f"插入 {data.get('inserted', 0)} 点，"
                                 f"跳过重复 {data.get('skipped_duplicates', 0)} 点")
                if data.get('warnings'):
                    row['status'] += '；警告: ' + '; '.join(data['warnings'])
            else:
                err = (data or {}).get('error') or f'HTTP {status}'
                if status == 422:
                    err += '（源缺 t0，请在 AJST 补录 t0 后重试）'
                row['error'] = err

    return rows


@login_required
@permission_required('stdweb.ajst_upload', raise_exception=True)
def ajst(request):
    context = {}

    if not settings.AJST_TOKEN:
        messages.warning(request, 'AJST 上传接口未配置（AJST_TOKEN 未设置），源解析与上传不可用。')

    form = forms.AJSTSelectForm(request.POST or None)
    context['form'] = form

    if request.method == 'POST':
        action = request.POST.get('action', 'preview')
        context['action'] = action

        if action in ('init', 'preview'):
            if form.is_valid():
                ids = parse_ids(form.cleaned_data.get('ids'))
                types = form.cleaned_data.get('types') or ['direct', 'subtracted']
                transient_id = form.cleaned_data.get('transient_id')

                rows = []
                for id in ids:
                    try:
                        task = models.Task.objects.get(id=id)
                    except models.Task.DoesNotExist:
                        rows.append({'task_id': id, 'types': '', 'selected': False, 'fatal': True,
                                     'error': '任务不存在'})
                        continue

                    task_rows = build_task_rows(task, types, transient_id=transient_id)
                    if not task_rows:
                        rows.append({'task_id': id, 'types': '/'.join(types), 'selected': False,
                                     'fatal': True, 'original_name': task.original_name,
                                     'error': '任务无对应的测光文件（target.vot / sub_target.vot）'})
                    rows.extend(task_rows)

                if settings.AJST_TOKEN:
                    resolve_rows(request, [r for r in rows if 'mjd' in r])

                context['rows'] = rows

        elif action == 'resolve':
            # Re-resolve from the current (possibly edited) form values
            rows = rows_from_post(request.POST)
            resolve_rows(request, rows)
            context['rows'] = rows

        elif action == 'upload':
            rows = rows_from_post(request.POST)

            if not settings.AJST_TOKEN:
                messages.error(request, 'AJST_TOKEN 未配置，无法上传。')
            elif form.is_valid():
                rows = upload_rows(request, rows,
                                   create_if_missing=form.cleaned_data.get('create_if_missing'),
                                   new_t0=form.cleaned_data.get('new_t0'))

                ok = sum(1 for r in rows if r.get('status'))
                failed = sum(1 for r in rows if r.get('error') and r.get('selected'))
                if ok:
                    messages.success(request, f'成功上传 {ok} 行测光数据至 AJST。')
                if failed:
                    messages.error(request, f'{failed} 行上传失败，详见各行错误信息。')

            context['rows'] = rows

    return TemplateResponse(request, 'ajst.html', context=context)
