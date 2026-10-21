#!/usr/bin/env node
// hide-check.mjs — 조립된 워커 프롬프트에서 리드 전용 수치 누출을 검사한다.
//
// 계약 (21-implementation-spec §2-6 ABI):
//   argv   : [--session <sf-id>] <조립된 워커 프롬프트 파일>...  |  --test
//   stdout : 누출 매치 목록 (파일:라인:패턴) · 면제 매치는 `면제(질문 원문)` 꼬리표로 따로 찍는다
//   exit   : 0=PASS · 1=FAIL(누출 또는 **검사기 사망**) · 2=스크립트 오류(**통과 취급 ❌**)
//
// 질문 원문 구간 면제 (10-21 · sf-1gg11zm 오탐 6건):
//   워커 브리프는 운영자 질문을 그대로 싣는다(mcp-server workerBrief `질의: …`). 질문은 운영자 입력이지
//   리드 전용 층이 아니다. 그런데 질문에 `15~60%`·`관련80%` 같은 주제 숫자가 있으면 `60|70|80 %` 패턴이
//   그걸 기준 누출로 잡았다. 그래서 **브리프 안에서 질문 줄을 그대로 옮긴 구간**에 완전히 들어간 매치만
//   면제한다. 값(토큰) 기준 면제가 아니다 — 질문에 60% 가 있어도 리드가 따로 적은 `합격선 60%` 는 잡힌다.
//   · 운영 면제 원천 = `--session` (서버 원장 start.question) **하나뿐**이다. `--question <파일>` 은 셀프테스트
//     안에서만 쓰고, 본 경로에서 받으면 exit 2 로 거부한다 — 리드가 만든 파일을 면제 원천으로 삼는 길을 닫는다.
//   · 8자 미만 질문 줄은 가림에 쓰지 않는다(짧은 줄이 넓게 가리는 것 방지 → 오탐 쪽으로 남김).
//   · 양성 fixture 생존 검사는 가림 없이 원문 그대로 본다.
//   · 면제는 조용히 하지 않는다 — 매치마다 stdout 에 찍고, 1건 이상이면 PASS·누출 줄에 `면제 N건 — 사람 확인` 을 찍는다.
//   · 이 검사는 자가 점검이다. 질문 자체에 기준 수치를 넣는 식의 의도적 우회는 못 막는다 → 면제 줄을 사람이 본다.
//   · 플래그가 없으면 종전과 같은 거동이다.
//   · 면제는 11개 패턴 전부에 적용된다(숫자층만이 아님) — 질문 줄은 어휘(예: weights)까지 포함해 운영자 입력이기 때문.
//   · (셀프테스트 경로) 질문 파일이 검사 대상 자신이면 거부 · `--question=값`·`--session=값` 등호형은 거부(조용히 무시 ❌).
//
// 금칙 패턴 SoT = references/scoring.md 머리의 `<!-- HIDE-TOKENS: ... -->` 선언부 **한 곳뿐**.
// 여기에 패턴을 복제하지 않는다 — 이중 관리는 한쪽만 갱신되는 순간 은닉이 조용히 얇아진다.
//
// ⚠️ **양성 대상 미검출 = exit 1.** 매 실행마다 고의 오염 fixture 를 함께 검사해
//    "0건 통과"와 "검사기가 죽어서 아무것도 못 봄"을 갈라낸다 — 이 둘은 화면상 같다.
//
// 외부 패키지 0 (node: 내장만).

import { readFileSync, existsSync, realpathSync, mkdtempSync, writeFileSync, mkdirSync, rmSync } from 'node:fs';
import { dirname, resolve, join } from 'node:path';
import { homedir, tmpdir } from 'node:os';
import { spawnSync } from 'node:child_process';
import { fileURLToPath } from 'node:url';

const SELF = fileURLToPath(import.meta.url);   // 셀프테스트가 본 경로를 하위 프로세스로 돌릴 때 쓴다(변이 사본이면 그 사본)
const SCRIPT_DIR = dirname(SELF);
const SKILL_DIR = resolve(SCRIPT_DIR, '..');
const SCORING = join(SKILL_DIR, 'references', 'scoring.md');
const POSITIVE = join(SKILL_DIR, 'fixtures', 'hide-positive.txt');

class ScriptError extends Error {}

/** 한글 조합형/분해형 차이로 검사가 새지 않게 — 비교 전 NFC 로 모은다. */
const nfc = s => s.normalize('NFC');

/** scoring.md 머리의 HIDE-TOKENS 블록 → 정규식 배열. */
function loadPatterns() {
  if (!existsSync(SCORING)) {
    throw new ScriptError(`패턴 SoT 부재 — ${SCORING}`);
  }
  const text = nfc(readFileSync(SCORING, 'utf8'));
  const m = text.match(/<!--\s*HIDE-TOKENS:\s*([\s\S]*?)-->/);
  if (!m) {
    throw new ScriptError('scoring.md 에 HIDE-TOKENS 선언부가 없음 — 패턴 0개로는 검사가 성립하지 않는다');
  }
  const out = [];
  for (const raw of m[1].split('\n')) {
    const line = raw.trim();
    if (!line || line.startsWith('#')) continue;
    try {
      out.push({ src: line, re: new RegExp(line, 'g') });
    } catch (err) {
      throw new ScriptError(`패턴 컴파일 실패 — /${line}/ : ${err.message}`);
    }
  }
  if (!out.length) {
    throw new ScriptError('HIDE-TOKENS 블록이 비어 있음 — 검사기가 아무것도 안 본다');
  }
  return out;
}

/** 가림에 쓰는 질문 줄의 최소 길이(NFC·trim 뒤 글자 수). 이보다 짧은 줄은 가리지 않는다. */
const QUESTION_MIN_LINE = 8;

/** 질문 원문 → 가림용 줄 목록(NFC·trim·짧은 줄 제외·중복 제거). */
function questionLines(question) {
  if (!question) return [];
  const seen = new Set();
  for (const raw of nfc(question).split('\n')) {
    const l = raw.trim();
    if ([...l].length >= QUESTION_MIN_LINE) seen.add(l);
  }
  return [...seen];
}

/** 한 줄 안에서 질문 줄을 그대로 옮긴 구간들 [start, end). */
function questionSpans(line, qLines) {
  const spans = [];
  for (const q of qLines) {
    let from = 0, at;
    while ((at = line.indexOf(q, from)) !== -1) {
      spans.push([at, at + q.length]);
      from = at + 1;
    }
  }
  return spans;
}

/** 매치 [s, e) 가 한 구간 안에 완전히 들어가는가. 걸치기만 하면 면제 아님. */
const insideSpan = (s, e, spans) => spans.some(([a, b]) => a <= s && e <= b);

/**
 * 한 텍스트에서 누출 매치 목록. label 은 출력용 경로/이름.
 * qLines 가 주어지면 질문 원문 구간 안의 매치는 exempt:true 로 표시한다(버리지 않는다).
 */
function scanText(text, label, patterns, qLines = []) {
  const hits = [];
  const lines = nfc(text).split('\n');
  lines.forEach((line, i) => {
    const spans = qLines.length ? questionSpans(line, qLines) : [];
    for (const { src, re } of patterns) {
      re.lastIndex = 0;
      let mm;
      while ((mm = re.exec(line)) !== null) {
        const exempt = spans.length > 0 && insideSpan(mm.index, mm.index + mm[0].length, spans);
        hits.push({ file: label, line: i + 1, pattern: src, match: mm[0], exempt });
        if (mm.index === re.lastIndex) re.lastIndex += 1;   // 폭 0 매치 무한루프 방어
      }
    }
  });
  return hits;
}

function scanFile(path, patterns, qLines = []) {
  if (!existsSync(path)) throw new ScriptError(`검사 대상 부재 — ${path}`);
  return scanText(readFileSync(path, 'utf8'), path, patterns, qLines);
}

const fmt = h => `${h.file}:${h.line}:${h.pattern} | match="${h.match}"${h.exempt ? ' | 면제(질문 원문)' : ''}`;

/** 면제 원천이 리드 전용 층이면 거부 — scoring.md 를 질문으로 넘기면 전부 가려진다. */
function guardQuestion(text, origin) {
  if (/HIDE-TOKENS/.test(text)) {
    throw new ScriptError(`질문 원천에 HIDE-TOKENS 선언부가 있다 — 리드 전용 층을 면제 원천으로 쓸 수 없다 (${origin})`);
  }
  if (!text.trim()) throw new ScriptError(`질문 원문이 비어 있다 (${origin})`);
  return text;
}

/** --question <파일> */
function questionFromFile(path) {
  if (!existsSync(path)) throw new ScriptError(`질문 파일 부재 — ${path}`);
  let same = false;
  try { same = realpathSync(path) === realpathSync(SCORING); } catch { /* scoring 부재는 loadPatterns 가 잡는다 */ }
  if (same) throw new ScriptError(`질문 파일이 scoring.md 다 — 리드 전용 층을 면제 원천으로 쓸 수 없다`);
  return guardQuestion(readFileSync(path, 'utf8'), path);
}

/** --session <sf-id> — 서버 원장(start 이벤트)의 question. 경로 규칙 = mcp-server stateDir(). */
function questionFromSession(id) {
  if (!/^sf-[A-Za-z0-9-]+$/.test(id)) throw new ScriptError(`세션 id 형식이 아니다 — ${id}`);
  const env = process.env.SEARCHFLOW_STATE_DIR;
  const base = env ? resolve(env) : join(homedir(), '.searchflow');
  const p = join(base, 'sessions', `${id}.jsonl`);
  if (!existsSync(p)) throw new ScriptError(`세션 원장 부재 — ${p}`);
  for (const raw of readFileSync(p, 'utf8').split('\n')) {
    if (!raw.trim()) continue;
    let ev;
    try { ev = JSON.parse(raw); } catch { continue; }
    if (ev && ev.event === 'start' && typeof ev.question === 'string') return guardQuestion(ev.question, p);
  }
  throw new ScriptError(`세션 원장에 start.question 이 없다 — ${p}`);
}

/** 본 경로가 --question 을 받았을 때의 안내(exit 2). */
const QUESTION_MAIN_REFUSED =
  '--question 은 셀프테스트(--test) 안에서만 받는다 — 운영 면제 원천은 --session <세션 id>(서버 원장 start.question) 하나뿐이다. ' +
  '원장이 없으면 플래그 없이 돌리고, 걸린 매치는 사람이 보고 판단한다';

/**
 * argv → { test, targets, question }. 값을 받는 플래그의 값이 대상으로 새지 않게 한다.
 * allowQuestion = 셀프테스트만 true. 본 경로는 --question 을 거부한다(운영 원천 = --session 뿐).
 */
function parseArgs(argv, { allowQuestion = false } = {}) {
  const out = { test: false, targets: [], question: null, questionPath: null };
  let qSources = 0;
  for (let i = 0; i < argv.length; i++) {
    const a = argv[i];
    if (a === '--test') { out.test = true; continue; }
    if (/^--(question|session)=/.test(a)) {
      throw new ScriptError(`${a.split('=')[0]} 는 등호형을 받지 않는다 — '${a.split('=')[0]} <값>' 으로 띄어 쓴다`);
    }
    if (a === '--question' && !allowQuestion) throw new ScriptError(QUESTION_MAIN_REFUSED);
    if (a === '--question' || a === '--session') {
      const v = argv[i + 1];
      if (v === undefined || v.startsWith('--')) throw new ScriptError(`${a} 뒤에 값이 없다`);
      i += 1;
      qSources += 1;
      out.question = a === '--question' ? questionFromFile(v) : questionFromSession(v);
      if (a === '--question') out.questionPath = v;
      continue;
    }
    if (a.startsWith('--')) continue;   // 종전 거동: 모르는 플래그는 대상에서 뺀다
    out.targets.push(a);
  }
  if (qSources > 1) throw new ScriptError('--question 과 --session 은 하나만 준다');
  // 질문 원천이 검사 대상 자신이면 그 파일 전체가 가려진다 — 거부.
  if (out.questionPath) {
    const rq = realpathSync(out.questionPath);
    for (const t of out.targets) {
      let rt = null;
      try { rt = realpathSync(t); } catch { /* 대상 부재는 scanFile 이 잡는다 */ }
      if (rt === rq) throw new ScriptError(`질문 파일이 검사 대상과 같다 — ${t} (자기 자신을 면제 원천으로 쓸 수 없다)`);
    }
  }
  return out;
}

/**
 * 양성 대조 — 고의 오염 fixture 가 잡히는지.
 * 잡히지 않으면 이 실행의 "0건"은 무의미하다(패턴 로드 실패·정규화 사고·fixture 표류).
 */
function livenessOrDie(patterns) {
  if (!existsSync(POSITIVE)) {
    throw new ScriptError(`양성 fixture 부재 — ${POSITIVE} (생존 증명 불가 = 검사 성립 안 함)`);
  }
  return scanFile(POSITIVE, patterns);
}

/**
 * 패턴별 커버리지 — fixture 가 밟지 않는 패턴은 **검증되지 않은 패턴**이다.
 * 전체 히트 ≥1 만 보면 어휘층 하나가 잡히는 것으로 수치층 사망을 덮는다(실제로 겪은 갭).
 * 본 경로에서는 stderr 경고(ABI 유지), `--test` 에서는 FAIL.
 */
function uncovered(patterns, posHits) {
  const seen = new Set(posHits.map(h => h.pattern));
  return patterns.filter(p => !seen.has(p.src)).map(p => p.src);
}

// ── 셀프테스트 ──────────────────────────────────────────────────────────────
function selfTest() {
  const results = [];
  let patterns;
  try {
    patterns = loadPatterns();
  } catch (err) {
    console.log(`FAIL  패턴 로드 — ${err.message}`);
    return 1;
  }
  results.push({ name: 'HIDE-TOKENS 패턴 로드 ≥1', ok: patterns.length >= 1, detail: `${patterns.length}개` });

  // 양성 대조 — 고의 오염이 잡히는가
  let pos = [], posErr = null;
  try { pos = livenessOrDie(patterns); } catch (err) { posErr = err.message; }
  results.push({
    name: '양성 대조 — 고의 오염 fixture 검출 ≥1',
    ok: pos.length >= 1,
    detail: posErr || `${pos.length}건 (0 이면 검사기 사망)`,
  });

  // 패턴별 커버리지 — 어휘층 히트로 수치층 사망을 덮지 못하게
  const gaps = posErr ? patterns.map(p => p.src) : uncovered(patterns, pos);
  results.push({
    name: '커버리지 — 선언 패턴 전종이 fixture 로 검증됨',
    ok: gaps.length === 0,
    detail: gaps.length ? `미검증 ${gaps.length}개: ${gaps.join(' / ')}` : `${patterns.length}/${patterns.length}`,
  });

  // 음성 대조 1 — 깨끗한 텍스트는 0건 (항상 FAIL 하는 검사기 배제)
  const clean = [
    '담당 축: f3 (반증) — 이 주장에 반대되는 증거를 찾는다.',
    '반환: findings[] + SELF-REPORT S1~S5 각 Y/N + 근거 1줄.',
    '취득은 references/acquisition.md 사다리를 따른다. 우회 금지.',
    '마감: 2026-07-30 13:00 KST 까지.',
  ].join('\n');
  const cleanHits = scanText(clean, '(clean)', patterns);
  results.push({
    name: '음성 대조 — 정상 워커 프롬프트 0건',
    ok: cleanHits.length === 0,
    detail: cleanHits.length ? cleanHits.map(fmt).join(' / ') : '0건',
  });

  // 음성 대조 2 — 버전·좌표 소수는 오탐 아님
  //
  // ⚠️ 여기서 `2.5배` 를 뺐다. 이 케이스는 **버전도 좌표도 아니다** — 그냥 소수 통계값이고,
  //    "정당한 통계 소수는 통과한다" 는 별개 주장인데 이 fixture 이름 아래 섞여 있었다.
  //    그리고 그 주장은 scoring.md THREATS 의 선언과 **반대**다: 정당한 소수도 잡히며,
  //    처방은 패턴을 좁히는 게 아니라 그 수치를 조립문에서 워커에게 넘기지 않는 것이다.
  //    구 열거형 패턴에서 `2.5` 가 통과했던 건 원칙이 아니라 **목록에 없어서**였다.
  //    (제거 ❌ 아래 별도 항목으로 옮겨 명시 선언한다 — 침묵 삭제는 커버리지 축소다.)
  const versionish = 'codex 0.146.0 / v1.20 / lat 100.15 / 항목 1.2.3';
  const vHits = scanText(versionish, '(version)', patterns);
  results.push({
    name: '음성 대조 — 버전·좌표 소수 오탐 0건',
    ok: vHits.length === 0,
    detail: vHits.length ? vHits.map(fmt).join(' / ') : '0건',
  });

  // 선언 항목 — 정당한 통계 소수도 **의도적으로** 잡힌다(오탐이 아니라 설계).
  // 이걸 PASS/FAIL 로 두지 않고 관측값으로 남기는 이유: 방향이 정책 결정이라 검사기가 판정할 게 아니다.
  // 판정을 뒤집고 싶으면 scoring.md THREATS 의 처방(수치를 워커에게 안 넘긴다)을 먼저 바꿔야 한다.
  const statish = scanText('교차 비율이 2.5배 늘었다 / 유효 응답 0.45 비중', '(stat)', patterns);
  results.push({
    name: '선언 — 정당한 통계 소수도 잡힌다(패턴 좁히기 ❌ · 조립문에서 안 넘기는 것이 처방)',
    ok: statish.length >= 1,
    detail: statish.length ? `${statish.length}건 검출 = 선언대로` : '0건 — 수치층이 통계 소수를 안 잡는다(범위가 좁아졌는지 확인)',
  });

  // 양성 대조 2 — NFD(분해형) 한글도 잡히는가 = 정규화가 실효인가
  const nfd = '이 조사의 문턱을 넘기면 종료'.normalize('NFD');
  const nfdHits = scanText(nfd, '(nfd)', patterns);
  results.push({
    name: '양성 대조 — NFD 분해형 한글 검출 (정규화 실효)',
    ok: nfdHits.length >= 1,
    detail: nfdHits.length ? nfdHits.map(h => h.pattern).join(',') : '0건 — normalize 가 안 걸리고 있다',
  });

  // 양성 대조 3 — 수치층이 실제로 문턱/가중치 소수를 잡는가
  const numeric = '합격은 0.80 이상, 이 축 비중은 .35 다.';
  const nHits = scanText(numeric, '(numeric)', patterns);
  results.push({
    name: '양성 대조 — 소수 수치층 검출 ≥2',
    ok: nHits.length >= 2,
    detail: `${nHits.length}건 ${nHits.map(h => h.match).join(',')}`,
  });

  // ── 질문 원문 구간 면제 (10-21 · sf-1gg11zm 오탐) ──
  // 원 사건 모양 그대로: 질문 줄에 주제 숫자 `15~60%`·`관련80%` 가 있고, 브리프가 `질의: ` 뒤에 그 줄을 싣는다.
  const q = [
    '지식 기반에서 새로운 가설을 발산시키는 방법은?',
    '씨앗: 중간교란→통과율 관리(15~60%); 도핑→관련80%·먼분야20%.',
  ].join('\n');
  const qLines = questionLines(q);
  const briefOnlyQ = `질의: ${q}\n담당 축: f1 — 공식 규격\n반환: findings 파일로 리드에게 돌려준다.`;

  // 음성 — 질문 원문 안의 60%·80% 는 누출이 아니다(면제로 보이게 남는다)
  const qNeg = scanText(briefOnlyQ, '(q-neg)', patterns, qLines);
  const qNegLeak = qNeg.filter(h => !h.exempt), qNegEx = qNeg.filter(h => h.exempt);
  results.push({
    name: '음성 대조 — 질문 원문 구간의 60%·80% 는 누출 0건(면제 ≥2 로 표시)',
    ok: qNegLeak.length === 0 && qNegEx.length >= 2,
    detail: `누출 ${qNegLeak.length}건 · 면제 ${qNegEx.length}건 ${qNegEx.map(h => h.match).join(',')}`,
  });

  // 대조 — 같은 브리프를 질문 없이 보면 종전처럼 잡힌다(면제가 실제로 일한 것인지)
  const qNoFlag = scanText(briefOnlyQ, '(q-noflag)', patterns);
  results.push({
    name: '대조 — 질문을 안 주면 같은 브리프가 종전대로 걸린다(≥2)',
    ok: qNoFlag.length >= 2 && qNoFlag.every(h => !h.exempt),
    detail: `${qNoFlag.length}건`,
  });

  // 양성 — 질문에 80% 가 있어도, 리드가 질문 밖에 적은 기준 문구는 잡힌다(값 면제가 아니라 구간 면제)
  const qPos = scanText(`${briefOnlyQ}\n이 축의 합격선은 80% 다.\n통과선 0.70 을 넘겨라.`, '(q-pos)', patterns, qLines);
  const qPosLeak = qPos.filter(h => !h.exempt);
  const posMatches = new Set(qPosLeak.map(h => h.match));
  results.push({
    name: '양성 대조 — 질문에 80% 가 있어도 질문 밖 기준 문구(합격선 80%·0.70)는 누출로 잡힌다',
    ok: posMatches.has('80%') && posMatches.has('0.70') && qPosLeak.some(h => h.match === '합격선'),
    detail: `누출 ${qPosLeak.length}건 ${qPosLeak.map(h => h.match).join(',')}`,
  });

  // 양성 — 질문 줄 일부만 옮기고 값을 덧붙인 구간은 면제 아님(걸치기 ❌)
  const partial = scanText('씨앗: 중간교란→통과율 관리(15~60%); 도핑→관련80%', '(q-partial)', patterns, qLines);
  results.push({
    name: '양성 대조 — 질문 줄을 끝까지 옮기지 않은 구간은 면제 안 함',
    ok: partial.filter(h => !h.exempt).length >= 1,
    detail: `누출 ${partial.filter(h => !h.exempt).length}건`,
  });

  // 양성 — 질문 줄 «전체»를 옮긴 뒤 같은 줄에 기준을 덧붙이면, 덧붙인 쪽은 잡힌다(구간 밖)
  //   insideSpan 을 «줄에 질문이 있으면 전부 면제»로 망가뜨리면 여기서 FAIL 한다(검증자 변이 D2).
  const qFull = qLines[1];
  const sameLine = scanText(`질의: ${qFull} 합격선 80%`, '(q-sameline)', patterns, qLines);
  const slLeak = sameLine.filter(h => !h.exempt).map(h => h.match);
  const slEx = sameLine.filter(h => h.exempt).map(h => h.match);
  results.push({
    name: '양성 대조 — 질문 줄 뒤 같은 줄에 덧붙인 기준(합격선 80%)은 잡고, 질문 구간 60%·80% 만 면제',
    ok: slLeak.includes('합격선') && slLeak.includes('80%') && slEx.length === 2,
    detail: `누출 ${slLeak.join(',')} · 면제 ${slEx.join(',')}`,
  });

  // 양성 — 매치가 질문 구간 경계에 걸치면 면제 아님
  //   insideSpan 을 «겹치면 면제»로 망가뜨리면 여기서 FAIL 한다(검증자 변이 D2).
  //   질문 줄이 `…관련8` 로 끝나고 브리프가 `…관련80%` 로 이어지면 `80%` 는 구간에 걸친다.
  const qEdge = '경계 시험용 질문 줄입니다 관련8';
  const edge = scanText(`${qEdge}0% 이상`, '(q-edge)', patterns, questionLines(qEdge));
  results.push({
    name: '양성 대조 — 질문 구간 경계에 걸친 매치는 면제 안 함',
    ok: edge.some(h => h.match === '80%' && !h.exempt),
    detail: edge.map(h => `${h.match}${h.exempt ? '(면제)' : ''}`).join(',') || '0건',
  });

  // 생존 — 질문을 줘도 양성 fixture 는 가림 없이 본다
  let posQ = [];
  try { posQ = livenessOrDie(patterns); } catch { /* 위 양성 대조가 이미 FAIL */ }
  results.push({
    name: '생존 — 질문 면제가 있어도 fixture 검출 수 불변(fixture 는 가리지 않는다)',
    ok: posQ.length === pos.length && posQ.length >= 1 && posQ.every(h => !h.exempt),
    detail: `${posQ.length}건 (기준 ${pos.length}건)`,
  });

  // 가드 — 리드 전용 층을 면제 원천으로 넘기면 스크립트 오류
  let guardOk = false, guardMsg = '';
  try { questionFromFile(SCORING); } catch (err) { guardOk = err instanceof ScriptError; guardMsg = err.message; }
  results.push({
    name: '가드 — scoring.md 를 질문으로 주면 거부(exit 2 경로)',
    ok: guardOk,
    detail: guardMsg || '거부 안 됨',
  });

  // 짧은 줄 — 8자 미만 질문 줄은 가림에 안 쓴다
  const shortQ = scanText('합격 60%', '(q-short)', patterns, questionLines('합격 60%'));
  results.push({
    name: `짧은 질문 줄(<${QUESTION_MIN_LINE}자)은 가림에 안 쓴다`,
    ok: shortQ.length >= 1 && shortQ.every(h => !h.exempt),
    detail: `${shortQ.length}건 · 면제 ${shortQ.filter(h => h.exempt).length}`,
  });

  // ── 본 경로 실행 시험 (하위 프로세스로 실제 CLI 를 돌린다) ──
  const tmp = mkdtempSync(join(tmpdir(), 'hide-check-selftest-'));
  try {
    const runSelf = (argv, env = {}) => spawnSync(process.execPath, [SELF, ...argv], {
      encoding: 'utf8', env: { ...process.env, ...env },
    });
    const qFile = join(tmp, 'question.txt');
    const briefQ = join(tmp, 'brief-q.txt');
    const briefPlain = join(tmp, 'brief-plain.txt');
    writeFileSync(qFile, q);
    writeFileSync(briefQ, `${briefOnlyQ}\n`);
    writeFileSync(briefPlain, '질의: 공개 문서에서 규격 변경 이력을 찾는다\n반환: findings 파일로 리드에게 돌려준다.\n');
    mkdirSync(join(tmp, 'sessions'));
    writeFileSync(join(tmp, 'sessions', 'sf-selftest1.jsonl'), `${JSON.stringify({ event: 'start', question: q })}\n`);
    const passLine = out => (out.split('\n').find(l => l.startsWith('PASS')) || '');

    // 본 경로 --question 거부 — exit 2 + 안내(--session 을 가리킨다)
    const rq = runSelf(['--question', qFile, briefQ]);
    results.push({
      name: '본 경로 — --question 은 거부한다(exit 2 · --session 안내)',
      ok: rq.status === 2 && rq.stderr.includes('--session') && rq.stderr.includes('셀프테스트'),
      detail: `exit=${rq.status} ${rq.stderr.split('\n')[0]}`,
    });

    // 본 경로 --session · 면제 ≥1 → PASS 줄에 «면제 N건 — 사람 확인»
    const rs = runSelf(['--session', 'sf-selftest1', briefQ], { SEARCHFLOW_STATE_DIR: tmp });
    const pl = passLine(rs.stdout);
    results.push({
      name: '본 경로 — --session 면제 ≥1 이면 PASS 줄에 «면제 N건 — 사람 확인»',
      ok: rs.status === 0 && /면제 2건 — 사람 확인/.test(pl),
      detail: `exit=${rs.status} ${pl || rs.stderr.split('\n')[0]}`,
    });

    // 음성 짝 — 면제 0건이면 «사람 확인» 을 찍지 않는다(문구가 늘 붙는 것이 아님)
    const r0 = runSelf(['--session', 'sf-selftest1', briefPlain], { SEARCHFLOW_STATE_DIR: tmp });
    const pl0 = passLine(r0.stdout);
    results.push({
      name: '본 경로 — 면제 0건이면 «사람 확인» 없음(음성 짝)',
      ok: r0.status === 0 && pl0.includes('면제 0건') && !pl0.includes('사람 확인'),
      detail: `exit=${r0.status} ${pl0 || r0.stderr.split('\n')[0]}`,
    });

    // 셀프테스트 경로만 --question 을 받는다 — 받되 자기 자신을 원천으로 주면 거부
    let accepted = false, selfRefused = false;
    try { accepted = parseArgs(['--question', qFile, briefQ], { allowQuestion: true }).question === q; } catch { /* accepted=false */ }
    try { parseArgs(['--question', qFile, qFile], { allowQuestion: true }); } catch (err) { selfRefused = err instanceof ScriptError; }
    results.push({
      name: '셀프테스트 경로 — --question 을 받고, 자기 자신 원천은 거부',
      ok: accepted && selfRefused,
      detail: `받음=${accepted} · 자기 자신 거부=${selfRefused}`,
    });
  } finally {
    rmSync(tmp, { recursive: true, force: true });
  }

  let failed = 0;
  for (const r of results) {
    console.log(`${r.ok ? 'PASS' : 'FAIL'}  ${r.name}\n      ${r.detail}`);
    if (!r.ok) failed += 1;
  }
  console.log(`\n${results.length - failed}/${results.length} PASS`);
  return failed === 0 ? 0 : 1;
}

// ── 본 경로 ─────────────────────────────────────────────────────────────────
try {
  const args = process.argv.slice(2);
  if (args.includes('--test')) process.exit(selfTest());

  const patterns = loadPatterns();
  const { targets, question } = parseArgs(args);
  if (!targets.length) {
    console.error('usage: hide-check.mjs [--session <sf-id>] <조립된 워커 프롬프트 파일>... | --test');
    process.exit(2);
  }
  const qLines = questionLines(question);

  // 생존 증명 먼저 — 죽은 검사기의 "0건"을 PASS 로 내보내지 않는다.
  const alive = livenessOrDie(patterns);
  if (alive.length === 0) {
    console.log(`검사기 사망 — 양성 fixture(${POSITIVE})에서 0건. 패턴 ${patterns.length}개가 로드됐는데도 고의 오염이 안 잡힌다.`);
    console.log('이번 실행의 결과는 신뢰할 수 없다(0건이 "깨끗함"이 아니라 "안 봄"일 수 있다).');
    process.exit(1);
  }

  const gaps = uncovered(patterns, alive);
  if (gaps.length) {
    process.stderr.write(
      `[hide-check] ⚠️ 미검증 패턴 ${gaps.length}개 — 양성 fixture 가 밟지 않는다: ${gaps.join(' / ')}\n` +
      `[hide-check]    이 패턴들의 "0건"은 생존이 증명되지 않은 0건이다. fixture 에 해당 줄을 추가할 것(--test 는 FAIL).\n`);
  }

  const all = [];
  for (const t of targets) all.push(...scanFile(t, patterns, qLines));
  const hits = all.filter(h => !h.exempt);
  const exempt = all.filter(h => h.exempt);

  // 면제는 조용히 하지 않는다 — 누출이든 PASS 든 면제 매치를 먼저 다 찍는다.
  for (const h of exempt) console.log(fmt(h));
  // 면제가 1건이라도 있으면 사람 확인을 요구한다 — 자가 점검이라 의도적 우회는 못 막는다.
  const exNote = !question ? ''
    : exempt.length
      ? ` · ⚠️ 면제 ${exempt.length}건 — 사람 확인(위 «면제(질문 원문)» 줄 · 질문 줄 ${qLines.length}개)`
      : ' · 면제 0건';

  if (hits.length) {
    for (const h of hits) console.log(fmt(h));
    console.log(`\n누출 ${hits.length}건${exNote} / 대상 ${targets.length}개 — 조립 경로가 리드 전용 층을 밟았다.`);
    process.exit(1);
  }

  console.log(`PASS — 누출 0건${exNote} (대상 ${targets.length}개 · 패턴 ${patterns.length}개 · 양성 대조 ${alive.length}건 검출로 생존 확인)`);
  process.exit(0);
} catch (err) {
  if (err instanceof ScriptError) {
    console.error(`[hide-check] 스크립트 오류 — ${err.message}`);
    console.error('exit 2 = 통과 취급 ❌. 사유를 기록하고 조립을 진행하지 않는다.');
    process.exit(2);
  }
  console.error(`[hide-check] 예상 밖 오류 — ${err && err.stack || err}`);
  process.exit(2);
}
