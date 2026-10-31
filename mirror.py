#!/usr/bin/env python3
"""Copies one AyahReel voice into this repository's releases, and checks
the copy against what was copied.

    python3 mirror.py plan all|<id>,<id>...   matrix for the workflow
    python3 mirror.py voice <id>              mirror one voice
    python3 mirror.py check <id>              read a mirrored voice back

What a voice's release (tag `voice-<id>`) holds:

  p<n>-<sha>.bin  the voice's files exactly as their hosts serve them,
                  one after another, at most PACK_MAX bytes a pack
  index.json      where each file is: {"files": {key: [pack, offset,
                  length]}, "packs": [...]}; key SSSAAA for a voice of
                  one file per verse, SSS for one of one file per surah
  sums.json       sha256 of each file, as it came from its host
  timing.json     the verse table its host publishes beside each surah
                  file, verse starts and ends only (surah voices)

The app reads a file from its pack with a Range request, so what it
gets is byte for byte what the host would have sent.
"""
import hashlib
import json
import os
import random
import subprocess
import sys
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
MANIFEST = json.loads((HERE / 'voices.json').read_text(encoding='utf-8'))
REPO = os.environ.get('REPO', 'Mulhimfy/ayahreel-audio')
WORK = Path(os.environ.get('WORK', '/tmp/mirror'))
PACK_MAX = 1536 * 1024 * 1024  # under GitHub's 2 GiB a file
UA = 'AyahReel-mirror (+https://github.com/%s)' % REPO
TAG = 'voice-{}'


def log(*a):
    print(*a, flush=True)


def voice(vid):
    for v in MANIFEST['voices']:
        if v['id'] == vid:
            return v
    sys.exit(f'no voice {vid}')


def files_of(v):
    """[(key, url)] in pack order."""
    if v['kind'] == 'ayah':
        return [(f'{s:03d}{a:03d}', f"{v['base']}/{s:03d}{a:03d}.mp3")
                for s, c in enumerate(MANIFEST['counts'], 1)
                for a in range(1, c + 1)]
    return sorted(v['files'].items())


def is_mp3(head):
    return head[:3] == b'ID3' or (len(head) > 1 and head[0] == 0xFF
                                  and head[1] & 0xE0 == 0xE0)


def fetch_all(pairs, out, parallel):
    """Downloads [(key, url)] into out/<key>.mp3 with curl, several at
    a time; returns {key: http code} of those that didn't land."""
    todo = [(k, u) for k, u in pairs if not (out / f'{k}.mp3').exists()]
    for round_ in range(1, 4):
        if not todo:
            return {}
        cfg = out / 'curl.cfg'
        with cfg.open('w') as f:
            for k, u in todo:
                f.write(f'url = "{u}"\noutput = "{out}/{k}.part"\n')
        log(f'  fetch round {round_}: {len(todo)} files, {parallel} at a time')
        res = subprocess.run(
            ['curl', '--parallel', '--parallel-max', str(parallel),
             '--fail', '--location', '--silent', '--show-error',
             '--retry', '5', '--retry-all-errors', '--retry-delay', '3',
             '--connect-timeout', '20', '--max-time', '1800',
             '--user-agent', UA,
             '--write-out', '%{http_code} %{filename_effective}\\n',
             '--config', str(cfg)],
            capture_output=True, text=True)
        codes = {}
        for line in res.stdout.splitlines():
            code, _, name = line.partition(' ')
            codes[Path(name).name.removesuffix('.part')] = code
        failed = {}
        for k, u in todo:
            part = out / f'{k}.part'
            ok = part.exists() and part.stat().st_size > 0
            if ok:
                with part.open('rb') as f:
                    ok = is_mp3(f.read(4))
                if not ok:
                    log(f'  {k}: not an mp3 ({u})')
            if ok:
                part.rename(out / f'{k}.mp3')
            else:
                part.unlink(missing_ok=True)
                failed[k] = codes.get(k, '?')
        todo = [(k, u) for k, u in todo if k in failed]
        if todo:
            log(f'  {len(todo)} not had: '
                f'{sorted(set(failed.values()))} e.g. {todo[0][1]}')
            if all(failed[k] == '404' for k, _ in todo):
                return failed
            parallel = max(2, parallel // 2)
            time.sleep(20 * round_)
    return failed


def get_json(url):
    for attempt in range(1, 7):
        try:
            req = urllib.request.Request(url, headers={'User-Agent': UA})
            with urllib.request.urlopen(req, timeout=60) as r:
                return json.loads(r.read().decode('utf-8'))
        except Exception as e:  # noqa: BLE001 — every failure is retried
            log(f'  table try {attempt}: {url} — {e}')
            time.sleep(3 * attempt)
    raise RuntimeError(f'table not had: {url}')


def stripped(kind, j):
    """A host's verse table with nothing but where each verse starts
    and ends — in the host's own shape, so the app reads it as it
    reads the host's."""
    if kind == 'mp3quran':
        if not isinstance(j, list) or not j:
            raise ValueError('not a list')
        return [{'ayah': r['ayah'], 'start_time': r['start_time'],
                 'end_time': r['end_time']} for r in j]
    seg = j.get('segments') if isinstance(j, dict) else None
    if not isinstance(seg, dict) or not seg:
        raise ValueError('no segments')
    return {'segments': {k: {'time_from': w['time_from'],
                             'time_to': w['time_to']}
                         for k, w in seg.items()}}


def gh(*args, check=True, capture=False):
    for attempt in range(1, 6):
        r = subprocess.run(['gh', *args], capture_output=True, text=True)
        if r.returncode == 0 or not check:
            return r.stdout if capture else r
        log(f'  gh {args[0]} {args[1] if len(args) > 1 else ""} try '
            f'{attempt}: {r.stderr.strip()[:300]}')
        time.sleep(15 * attempt)
    raise RuntimeError(f'gh {" ".join(args[:3])} failed')


def sha_of(path):
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        for b in iter(lambda: f.read(1 << 20), b''):
            h.update(b)
    return h.hexdigest()


def public_url(vid, name):
    return f'https://github.com/{REPO}/releases/download/{TAG.format(vid)}/{name}'


def read_back(url, want_sha, want_size):
    h = hashlib.sha256()
    n = 0
    req = urllib.request.Request(url, headers={'User-Agent': UA})
    with urllib.request.urlopen(req, timeout=120) as r:
        for b in iter(lambda: r.read(1 << 20), b''):
            h.update(b)
            n += len(b)
    if n != want_size or h.hexdigest() != want_sha:
        raise RuntimeError(f'{url}: read back {n} B {h.hexdigest()[:12]}, '
                           f'made {want_size} B {want_sha[:12]}')


def range_read(url, start, length):
    req = urllib.request.Request(url, headers={
        'User-Agent': UA, 'Range': f'bytes={start}-{start + length - 1}'})
    with urllib.request.urlopen(req, timeout=120) as r:
        if r.status != 206:
            raise RuntimeError(f'{url}: {r.status} to a range')
        return r.read()


def mirror(vid):
    v = voice(vid)
    t0 = time.time()
    out = WORK / vid
    out.mkdir(parents=True, exist_ok=True)
    pairs = files_of(v)
    log(f'{vid}: {v["kind"]} voice, {len(pairs)} files')
    failed = fetch_all(pairs, out, 8 if v['kind'] == 'ayah' else 4)
    errors = {k: c for k, c in failed.items() if c != '404'}
    missing = sorted(k for k, c in failed.items() if c == '404')
    if missing:
        log(f'  {len(missing)} files the host doesn\'t have: {missing[:20]}')
    if errors:
        sys.exit(f'{vid}: {len(errors)} files not had: '
                 f'{sorted(errors.items())[:20]}')
    if v['kind'] == 'surah' and missing:
        # The app offers every surah it lists; a file gone from the host
        # is exactly what the mirror is for, so the voice isn't done.
        sys.exit(f'{vid}: the host lost {missing}')

    timing = {}
    if v['kind'] == 'surah' and v.get('timing'):
        for key, url in sorted(v['tables'].items()):
            timing[key] = stripped(v['timing']['kind'], get_json(url))
        log(f'  {len(timing)} verse tables')

    # Packs, in order; each file appended whole and let go.
    packs, index, sums = [], {}, {}
    cur, cur_path, cur_size, cur_hash = None, None, 0, None

    def close_pack():
        nonlocal cur, cur_path, cur_size, cur_hash
        if cur is None:
            return
        cur.close()
        sha = cur_hash.hexdigest()
        name = f'p{len(packs) + 1}-{sha[:10]}.bin'
        final = out / name
        cur_path.rename(final)
        packs.append({'name': name, 'size': cur_size, 'sha256': sha,
                      'path': str(final)})
        cur = None

    for key, _ in pairs:
        f = out / f'{key}.mp3'
        if not f.exists():
            continue
        size = f.stat().st_size
        if cur is not None and cur_size + size > PACK_MAX:
            close_pack()
        if cur is None:
            cur_path = out / f'pack{len(packs) + 1}.tmp'
            cur = cur_path.open('wb')
            cur_size, cur_hash = 0, hashlib.sha256()
        data = f.read_bytes()
        sums[key] = hashlib.sha256(data).hexdigest()
        index[key] = [len(packs), cur_size, size]
        cur.write(data)
        cur_hash.update(data)
        cur_size += size
        f.unlink()
    close_pack()
    total = sum(p['size'] for p in packs)
    log(f'  {len(index)} files in {len(packs)} packs, {total / 2**30:.2f} GiB')

    made = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
    meta = {'v': 1, 'voice': vid, 'kind': v['kind'], 'made': made,
            'packs': [{k: p[k] for k in ('name', 'size', 'sha256')}
                      for p in packs],
            'files': index, 'missing': missing}
    (out / 'index.json').write_text(json.dumps(meta, separators=(',', ':')))
    (out / 'sums.json').write_text(json.dumps(sums, separators=(',', ':')))
    extra = ['index.json', 'sums.json']
    if timing:
        (out / 'timing.json').write_text(
            json.dumps(timing, separators=(',', ':')))
        extra.append('timing.json')

    tag = TAG.format(vid)
    if gh('release', 'view', tag, '--repo', REPO, check=False).returncode:
        gh('release', 'create', tag, '--repo', REPO, '--title', vid,
           '--notes', f'Recitation files of the AyahReel voice `{vid}`, as '
           f'their hosts serve them (see voices.json).')
    before = json.loads(gh('release', 'view', tag, '--repo', REPO,
                           '--json', 'assets', capture=True))['assets']
    have = {a['name']: a for a in before}
    for p in packs:
        if p['name'] in have and have[p['name']]['size'] == p['size']:
            log(f'  {p["name"]} already up')
        else:
            log(f'  uploading {p["name"]} ({p["size"] / 2**20:.0f} MiB)')
            gh('release', 'upload', tag, p['path'], '--repo', REPO,
               '--clobber')
        read_back(public_url(vid, p['name']), p['sha256'], p['size'])
        log(f'  {p["name"]} read back, same bytes')
        Path(p['path']).unlink()
    # The index last: until it is up, the old one names packs still there.
    gh('release', 'upload', tag, *[str(out / n) for n in extra],
       '--repo', REPO, '--clobber')
    keep = {p['name'] for p in packs} | set(extra)
    for name in have:
        if name not in keep:
            gh('release', 'delete-asset', tag, name, '--repo', REPO, '--yes')
            log(f'  old {name} deleted')
    check(vid, quick=True)
    log(f'{vid}: done in {(time.time() - t0) / 60:.1f} min')
    summary = os.environ.get('GITHUB_STEP_SUMMARY')
    if summary:
        with open(summary, 'a') as f:
            f.write(f'| {vid} | {v["kind"]} | {len(index)} | {len(missing)} '
                    f'| {total / 2**30:.2f} GiB | {len(timing)} |\n')


def check(vid, quick=False):
    """Reads the voice's index from its public address and checks files
    in it, by Range as the app reads them, against the sums made when
    they came from their hosts: a sample when [quick], all otherwise."""
    def get(name):
        req = urllib.request.Request(public_url(vid, name),
                                     headers={'User-Agent': UA})
        with urllib.request.urlopen(req, timeout=120) as r:
            return json.loads(r.read().decode('utf-8'))
    meta, sums = get('index.json'), get('sums.json')
    keys = sorted(meta['files'])
    if quick:
        keys = random.sample(keys, min(8, len(keys))) + [keys[0], keys[-1]]
    for key in keys:
        p, off, n = meta['files'][key]
        data = range_read(public_url(vid, meta['packs'][p]['name']), off, n)
        if hashlib.sha256(data).hexdigest() != sums[key] or not is_mp3(data):
            raise RuntimeError(f'{vid} {key}: mirror bytes differ')
    log(f'  {vid}: {len(keys)} files read by range, all as their hosts sent')


def plan(arg):
    ids = [v['id'] for v in MANIFEST['voices']]
    if arg != 'all':
        want = [x.strip() for x in arg.split(',') if x.strip()]
        bad = [x for x in want if x not in ids]
        if bad:
            sys.exit(f'unknown voices {bad}')
        ids = want
    else:
        # Hosts taken in turn, so no one of them carries every job at once.
        a = [v['id'] for v in MANIFEST['voices'] if v['kind'] == 'ayah']
        b = [v['id'] for v in MANIFEST['voices'] if v['kind'] == 'surah']
        ids = [x for pair in zip(b, a + [None] * len(b)) for x in pair if x]
    print('voices=' + json.dumps(ids))


if __name__ == '__main__':
    cmd, arg = sys.argv[1], sys.argv[2]
    {'plan': plan, 'voice': mirror, 'check': check}[cmd](arg)
