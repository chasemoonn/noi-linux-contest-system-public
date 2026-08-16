/**
 * Hydro 5.0.1 addon: receive collected submissions from the orchestrator.
 * The implementation only uses exports present in hydrooj@5.0.1 plugin-api.
 */
const crypto = require('crypto');
const fs = require('fs');
const path = require('path');
const {
    BadRequestError,
    ContestModel,
    ContestNotLiveError,
    ContestNotAttendedError,
    CreateError,
    DocumentModel,
    DomainModel,
    Handler,
    InvalidTokenError,
    MessageModel,
    ObjectId,
    ProblemModel,
    ProblemNotFoundError,
    RecordModel,
    SettingModel,
    StorageModel,
    UserFacingError,
} = require('hydrooj');

const OrchestratorCodeTooLargeError = CreateError(
    'OrchestratorCodeTooLargeError',
    UserFacingError,
    'Submitted source code is too large.',
    413,
);
const OrchestratorSubmissionConflictError = CreateError(
    'OrchestratorSubmissionConflictError',
    UserFacingError,
    'The submission id was already used for different source code.',
    409,
);
const OrchestratorNotificationConflictError = CreateError(
    'OrchestratorNotificationConflictError',
    UserFacingError,
    'The notification id was already used for different seat information.',
    409,
);
const OrchestratorProblemDraftConflictError = CreateError(
    'OrchestratorProblemDraftConflictError',
    UserFacingError,
    'The file-I/O draft conflicts with the current contest or operation journal.',
    409,
);
const OrchestratorProblemDraftBlockedError = CreateError(
    'OrchestratorProblemDraftBlockedError',
    UserFacingError,
    'The contest cannot be changed to private file-I/O problems.',
    422,
);
const OrchestratorProblemDraftTooLargeError = CreateError(
    'OrchestratorProblemDraftTooLargeError',
    UserFacingError,
    'The problem snapshot is too large to verify safely.',
    413,
);
const OrchestratorMaterialConflictError = CreateError(
    'OrchestratorMaterialConflictError',
    UserFacingError,
    'The contest material publication conflicts with the current contest.',
    409,
);
const OrchestratorSubmissionAmbiguousError = CreateError(
    'OrchestratorSubmissionAmbiguousError',
    UserFacingError,
    'The OJ record result is ambiguous; automatic replay is blocked.',
    409,
);
const OrchestratorMaterialBlockedError = CreateError(
    'OrchestratorMaterialBlockedError',
    UserFacingError,
    'The contest material cannot be published after the contest has started.',
    422,
);
const OrchestratorMaterialTooLargeError = CreateError(
    'OrchestratorMaterialTooLargeError',
    UserFacingError,
    'The contest material publication is too large.',
    413,
);

function loadToken() {
    if (process.env.ORCHESTRATOR_TOKEN) return process.env.ORCHESTRATOR_TOKEN;
    const path = process.env.ORCHESTRATOR_TOKEN_FILE
        || '/root/.hydro/orchestrator-token';
    try {
        return fs.readFileSync(path, 'utf8').trim();
    } catch {
        return '';
    }
}

const TOKEN = loadToken();
const DOMAIN = process.env.ORCHESTRATOR_DOMAIN || 'system';
const MAX_CODE_BYTES = Number(process.env.ORCHESTRATOR_MAX_CODE_BYTES || 512 * 1024);
const IDEMPOTENCY_FILE = process.env.ORCHESTRATOR_IDEMPOTENCY_FILE
    || '/root/.hydro/orchestrator-idempotency.json';
const IDEMPOTENCY_MAX_ENTRIES = Number(
    process.env.ORCHESTRATOR_IDEMPOTENCY_MAX_ENTRIES || 20000,
);
const NOTIFICATION_IDEMPOTENCY_FILE = process.env.ORCHESTRATOR_NOTIFICATION_IDEMPOTENCY_FILE
    || '/root/.hydro/orchestrator-notifications.json';
const NOTIFICATION_IDEMPOTENCY_MAX_ENTRIES = Number(
    process.env.ORCHESTRATOR_NOTIFICATION_IDEMPOTENCY_MAX_ENTRIES || 20000,
);
const PROBLEM_DRAFT_IDEMPOTENCY_FILE = process.env.ORCHESTRATOR_PROBLEM_DRAFT_IDEMPOTENCY_FILE
    || '/root/.hydro/orchestrator-problem-drafts.json';
const PROBLEM_DRAFT_IDEMPOTENCY_MAX_ENTRIES = Number(
    process.env.ORCHESTRATOR_PROBLEM_DRAFT_IDEMPOTENCY_MAX_ENTRIES || 2000,
);
const MATERIAL_IDEMPOTENCY_FILE = process.env.ORCHESTRATOR_MATERIAL_IDEMPOTENCY_FILE
    || '/root/.hydro/orchestrator-material-publications.json';
const MATERIAL_IDEMPOTENCY_MAX_ENTRIES = Number(
    process.env.ORCHESTRATOR_MATERIAL_IDEMPOTENCY_MAX_ENTRIES || 2000,
);
const MATERIAL_MAX_BYTES = Number(
    process.env.ORCHESTRATOR_MATERIAL_MAX_BYTES || 64 * 1024 * 1024,
);
const PROBLEM_DRAFT_MAX_PROBLEMS = Number(
    process.env.ORCHESTRATOR_PROBLEM_DRAFT_MAX_PROBLEMS || 20,
);
const PROBLEM_DRAFT_MAX_FILES = Number(
    process.env.ORCHESTRATOR_PROBLEM_DRAFT_MAX_FILES || 10000,
);
const PROBLEM_DRAFT_MAX_HASH_BYTES = Number(
    process.env.ORCHESTRATOR_PROBLEM_DRAFT_MAX_HASH_BYTES || 2 * 1024 * 1024 * 1024,
);
const PROBLEM_DRAFT_MAX_LINE_BYTES = Number(
    process.env.ORCHESTRATOR_PROBLEM_DRAFT_MAX_LINE_BYTES || 16 * 1024 * 1024,
);
const PROBLEM_DRAFT_CLONE_VERIFY_ATTEMPTS = 10;
const PROBLEM_DRAFT_CLONE_VERIFY_BASE_DELAY_MS = 25;
const PROBLEM_DRAFT_CLONE_VERIFY_MAX_DELAY_MS = 500;
const NOTIFICATION_ALLOWED_HOSTS = new Set(
    String(process.env.ORCHESTRATOR_NOTIFY_ALLOWED_HTTPS_HOSTS || '')
        .split(',')
        .map((value) => value.trim().toLowerCase().replace(/\.$/, ''))
        .filter(Boolean),
);
const NOTIFICATION_FIELDS = new Set([
    'notification_id',
    'purpose',
    'uid',
    'contest_title',
    'desktop_url',
    'candidate',
    'student_password',
    'available_at',
]);
const MATERIAL_FIELDS = new Set([
    'publication_id',
    'tid',
    'revision',
    'attachments',
]);
const MATERIAL_ATTACHMENT_FIELDS = new Set([
    'name',
    'sha256',
    'content_base64',
]);
const MATERIAL_ATTACHMENT_NAMES = new Set([
    '01_比赛题面.pdf',
    '02_辅助自测数据.tar.gz',
]);
const PROBLEM_DRAFT_FIELDS = new Set([
    'action',
    'tid',
    'problems',
    'operation_id',
    'approval_id',
    'preflight_id',
]);
const PROBLEM_DRAFT_PROBLEM_FIELDS = new Set(['pid', 'slug']);
const CONFIG_FILENAMES = new Set([
    'config.yaml',
    'config.yml',
    'Config.yaml',
    'Config.yml',
]);
const SEAT_READY_MESSAGE = [
    '【NOI Linux 考试桌面已开放】',
    '',
    '比赛：{contest}',
    '开放时间：{available_at}',
    '',
    '请关闭以前的远程桌面标签页，使用下面的个人专属入口。',
    '桌面入口：{desktop:link}',
    '',
    '准考证号：{candidate}',
    '登录密码：{password}',
    '',
    '请勿把个人链接、准考证号或密码转发给其他同学。',
].join('\n');

function isExactDnsHostname(value) {
    if (value.length > 253
        || !value.includes('.')
        || /^\d+(?:\.\d+){3}$/.test(value)) return false;
    return value.split('.').every((label) => (
        label.length > 0
        && label.length <= 63
        && /^[a-z0-9](?:[a-z0-9-]*[a-z0-9])?$/.test(label)
    ));
}

function loadIdempotencyState() {
    try {
        const parsed = JSON.parse(fs.readFileSync(IDEMPOTENCY_FILE, 'utf8'));
        if (parsed?.version !== 1 || typeof parsed.entries !== 'object' || !parsed.entries) {
            throw new Error('unsupported idempotency file format');
        }
        return parsed;
    } catch (error) {
        if (error?.code === 'ENOENT') return { version: 1, entries: {} };
        throw new Error(`cannot load orchestrator idempotency state: ${error.message}`);
    }
}

const idempotencyState = loadIdempotencyState();
const inFlight = new Map();

function loadNotificationIdempotencyState() {
    try {
        const parsed = JSON.parse(fs.readFileSync(NOTIFICATION_IDEMPOTENCY_FILE, 'utf8'));
        if (parsed?.version !== 1 || typeof parsed.entries !== 'object' || !parsed.entries) {
            throw new Error('unsupported notification idempotency file format');
        }
        return parsed;
    } catch (error) {
        if (error?.code === 'ENOENT') return { version: 1, entries: {} };
        throw new Error(`cannot load orchestrator notification idempotency state: ${error.message}`);
    }
}

const notificationIdempotencyState = loadNotificationIdempotencyState();
const notificationInFlight = new Map();

function loadProblemDraftIdempotencyState() {
    try {
        const parsed = JSON.parse(fs.readFileSync(PROBLEM_DRAFT_IDEMPOTENCY_FILE, 'utf8'));
        if (parsed?.version !== 1 || typeof parsed.entries !== 'object' || !parsed.entries) {
            throw new Error('unsupported problem draft idempotency file format');
        }
        return parsed;
    } catch (error) {
        if (error?.code === 'ENOENT') return { version: 1, entries: {} };
        throw new Error(`cannot load orchestrator problem draft state: ${error.message}`);
    }
}

const problemDraftIdempotencyState = loadProblemDraftIdempotencyState();
const problemDraftInFlight = new Map();
const contestDraftMutations = new Set();
const contestSubmissionLeases = new Map();
const submissionResolutionInFlight = new Map();

function loadMaterialIdempotencyState() {
    try {
        const parsed = JSON.parse(fs.readFileSync(MATERIAL_IDEMPOTENCY_FILE, 'utf8'));
        if (parsed?.version !== 1 || typeof parsed.entries !== 'object' || !parsed.entries) {
            throw new Error('unsupported material publication state format');
        }
        return parsed;
    } catch (error) {
        if (error?.code === 'ENOENT') return { version: 1, entries: {} };
        throw new Error(`cannot load orchestrator material publication state: ${error.message}`);
    }
}

const materialIdempotencyState = loadMaterialIdempotencyState();
const materialInFlight = new Map();
const contestMaterialMutations = new Set();

function pruneIdempotencyState() {
    const entries = Object.entries(idempotencyState.entries);
    if (entries.length <= IDEMPOTENCY_MAX_ENTRIES) return;
    const removable = entries
        .filter(([, entry]) => entry.complete)
        .sort((left, right) => String(left[1].createdAt).localeCompare(String(right[1].createdAt)));
    const removeCount = entries.length - IDEMPOTENCY_MAX_ENTRIES;
    for (const [submissionId] of removable.slice(0, removeCount)) {
        delete idempotencyState.entries[submissionId];
    }
}

function saveIdempotencyState() {
    pruneIdempotencyState();
    const directory = path.dirname(IDEMPOTENCY_FILE);
    fs.mkdirSync(directory, { recursive: true, mode: 0o700 });
    const tempPath = `${IDEMPOTENCY_FILE}.${process.pid}.${crypto.randomBytes(8).toString('hex')}.tmp`;
    let handle;
    try {
        handle = fs.openSync(tempPath, 'wx', 0o600);
        fs.writeFileSync(handle, `${JSON.stringify(idempotencyState)}\n`, 'utf8');
        fs.fsyncSync(handle);
        fs.closeSync(handle);
        handle = undefined;
        fs.renameSync(tempPath, IDEMPOTENCY_FILE);
        fs.chmodSync(IDEMPOTENCY_FILE, 0o600);
        if (process.platform !== 'win32') {
            const directoryHandle = fs.openSync(directory, 'r');
            try {
                fs.fsyncSync(directoryHandle);
            } finally {
                fs.closeSync(directoryHandle);
            }
        }
    } finally {
        if (handle !== undefined) fs.closeSync(handle);
        try {
            fs.unlinkSync(tempPath);
        } catch (error) {
            if (error?.code !== 'ENOENT') throw error;
        }
    }
}

function pruneNotificationIdempotencyState() {
    const entries = Object.entries(notificationIdempotencyState.entries);
    if (entries.length <= NOTIFICATION_IDEMPOTENCY_MAX_ENTRIES) return;
    const removable = entries
        .filter(([, entry]) => entry.complete)
        .sort((left, right) => String(left[1].createdAt).localeCompare(String(right[1].createdAt)));
    const removeCount = entries.length - NOTIFICATION_IDEMPOTENCY_MAX_ENTRIES;
    for (const [notificationId] of removable.slice(0, removeCount)) {
        delete notificationIdempotencyState.entries[notificationId];
    }
}

function saveNotificationIdempotencyState() {
    pruneNotificationIdempotencyState();
    const directory = path.dirname(NOTIFICATION_IDEMPOTENCY_FILE);
    fs.mkdirSync(directory, { recursive: true, mode: 0o700 });
    const tempPath = `${NOTIFICATION_IDEMPOTENCY_FILE}.${process.pid}.${crypto.randomBytes(8).toString('hex')}.tmp`;
    try {
        fs.writeFileSync(tempPath, `${JSON.stringify(notificationIdempotencyState)}\n`, {
            encoding: 'utf8',
            flag: 'wx',
            mode: 0o600,
        });
        fs.renameSync(tempPath, NOTIFICATION_IDEMPOTENCY_FILE);
        fs.chmodSync(NOTIFICATION_IDEMPOTENCY_FILE, 0o600);
    } finally {
        try {
            fs.unlinkSync(tempPath);
        } catch (error) {
            if (error?.code !== 'ENOENT') throw error;
        }
    }
}

function pruneProblemDraftIdempotencyState() {
    const entries = Object.entries(problemDraftIdempotencyState.entries);
    if (entries.length <= PROBLEM_DRAFT_IDEMPOTENCY_MAX_ENTRIES) return;
    const removable = entries
        .filter(([, entry]) => entry.complete)
        .sort((left, right) => String(left[1].createdAt).localeCompare(String(right[1].createdAt)));
    const removeCount = entries.length - PROBLEM_DRAFT_IDEMPOTENCY_MAX_ENTRIES;
    for (const [operationId] of removable.slice(0, removeCount)) {
        delete problemDraftIdempotencyState.entries[operationId];
    }
}

function saveProblemDraftIdempotencyState() {
    pruneProblemDraftIdempotencyState();
    const directory = path.dirname(PROBLEM_DRAFT_IDEMPOTENCY_FILE);
    fs.mkdirSync(directory, { recursive: true, mode: 0o700 });
    const tempPath = `${PROBLEM_DRAFT_IDEMPOTENCY_FILE}.${process.pid}.${crypto.randomBytes(8).toString('hex')}.tmp`;
    try {
        fs.writeFileSync(tempPath, `${JSON.stringify(problemDraftIdempotencyState)}\n`, {
            encoding: 'utf8',
            flag: 'wx',
            mode: 0o600,
        });
        fs.renameSync(tempPath, PROBLEM_DRAFT_IDEMPOTENCY_FILE);
        fs.chmodSync(PROBLEM_DRAFT_IDEMPOTENCY_FILE, 0o600);
    } finally {
        try {
            fs.unlinkSync(tempPath);
        } catch (error) {
            if (error?.code !== 'ENOENT') throw error;
        }
    }
}

function pruneMaterialIdempotencyState() {
    const entries = Object.entries(materialIdempotencyState.entries);
    if (entries.length <= MATERIAL_IDEMPOTENCY_MAX_ENTRIES) return;
    const removable = entries
        .filter(([, entry]) => entry.complete)
        .sort((left, right) => String(left[1].createdAt).localeCompare(String(right[1].createdAt)));
    const removeCount = entries.length - MATERIAL_IDEMPOTENCY_MAX_ENTRIES;
    for (const [publicationId] of removable.slice(0, removeCount)) {
        delete materialIdempotencyState.entries[publicationId];
    }
}

function saveMaterialIdempotencyState() {
    pruneMaterialIdempotencyState();
    const directory = path.dirname(MATERIAL_IDEMPOTENCY_FILE);
    fs.mkdirSync(directory, { recursive: true, mode: 0o700 });
    const temporary = `${MATERIAL_IDEMPOTENCY_FILE}.${process.pid}.${crypto.randomBytes(8).toString('hex')}.tmp`;
    let handle;
    try {
        handle = fs.openSync(temporary, 'wx', 0o600);
        fs.writeFileSync(handle, `${JSON.stringify(materialIdempotencyState)}\n`, 'utf8');
        fs.fsyncSync(handle);
        fs.closeSync(handle);
        handle = undefined;
        fs.renameSync(temporary, MATERIAL_IDEMPOTENCY_FILE);
        fs.chmodSync(MATERIAL_IDEMPOTENCY_FILE, 0o600);
        if (process.platform !== 'win32') {
            const directoryHandle = fs.openSync(directory, 'r');
            try {
                fs.fsyncSync(directoryHandle);
            } finally {
                fs.closeSync(directoryHandle);
            }
        }
    } finally {
        if (handle !== undefined) fs.closeSync(handle);
        try {
            fs.unlinkSync(temporary);
        } catch (error) {
            if (error?.code !== 'ENOENT') throw error;
        }
    }
}

function payloadFingerprint(tid, uid, pid, lang, code) {
    return crypto.createHash('sha256')
        .update(JSON.stringify([tid, uid, pid, lang, code]))
        .digest('hex');
}

function submissionContestDocId(value) {
    const text = String(value);
    return ObjectId.isValid(text) ? new ObjectId(text) : value;
}

async function completeSubmissionEntry(entry, contestDocId, problemDocId, uid) {
    const rid = ObjectId.isValid(entry.rid) ? new ObjectId(entry.rid) : entry.rid;
    if (!entry.statusUpdated) {
        await ContestModel.updateStatus(DOMAIN, contestDocId, uid, rid, problemDocId);
        entry.statusUpdated = true;
        saveIdempotencyState();
    }
    if (!entry.problemCounted) {
        await ProblemModel.inc(DOMAIN, problemDocId, 'nSubmit', 1);
        entry.problemCounted = true;
        saveIdempotencyState();
    }
    if (!entry.userCounted) {
        await DomainModel.incUserInDomain(DOMAIN, uid, 'nSubmit');
        entry.userCounted = true;
        saveIdempotencyState();
    }
    entry.complete = true;
    saveIdempotencyState();
    return entry.rid;
}

function recordMatchesSubmissionEntry(record, submissionId, entry) {
    if (!record || !entry.publicPid || !entry.lang) return false;
    const recordFingerprint = payloadFingerprint(
        String(entry.contestDocId),
        Number(entry.uid),
        String(entry.publicPid),
        String(entry.lang),
        String(record.code || '').replace(/\r\n/g, '\n'),
    );
    return String(record.domainId || '') === DOMAIN
        && String(record.contest || '') === String(entry.contestDocId)
        && Number(record.pid) === Number(entry.problemDocId)
        && Number(record.uid) === Number(entry.uid)
        && String(record.lang || '') === String(entry.lang)
        && recordFingerprint === entry.fingerprint
        && record.files?.orchestratorSubmissionId === submissionId
        && record.files?.orchestratorPayloadSha256 === entry.fingerprint
        // A record insert without a judge task must not be reported as
        // delivered. Waiting for judgeAt proves Hydro actually processed it.
        && record.judgeAt instanceof Date;
}

async function resolveAmbiguousSubmission(submissionId, entry) {
    // Older journals did not put an exact, private correlation marker on the
    // record. They cannot be resolved safely by time/user/problem heuristics.
    if (!entry.publicPid || !entry.lang) {
        if (entry.complete && entry.rid) return { status: 'resolved', rid: entry.rid };
        return { status: 'unsupported' };
    }

    const candidates = await RecordModel.coll.find({
        domainId: DOMAIN,
        contest: submissionContestDocId(entry.contestDocId),
        pid: entry.problemDocId,
        uid: entry.uid,
        lang: entry.lang,
        'files.orchestratorSubmissionId': submissionId,
        'files.orchestratorPayloadSha256': entry.fingerprint,
    }).project({
        _id: 1,
        code: 1,
        contest: 1,
        domainId: 1,
        files: 1,
        judgeAt: 1,
        lang: 1,
        pid: 1,
        uid: 1,
    }).limit(2).toArray();
    if (!candidates.length) return { status: 'missing' };
    if (candidates.length !== 1) {
        return { status: 'multiple' };
    }
    if (!recordMatchesSubmissionEntry(candidates[0], submissionId, entry)) {
        return { status: 'pending' };
    }

    const candidateRid = String(candidates[0]._id);
    if (entry.rid && String(entry.rid) !== candidateRid) return { status: 'multiple' };
    // Even completed journal entries are correlated against the live record
    // collection on every status query. This makes the read-only endpoint a
    // uniqueness proof for qualification and recovery, rather than merely a
    // replay of an old local receipt.
    if (entry.complete && entry.rid) return { status: 'resolved', rid: candidateRid };

    entry.rid = candidateRid;
    entry.phase = 'record_created';
    saveIdempotencyState();
    const rid = await completeSubmissionEntry(
        entry,
        submissionContestDocId(entry.contestDocId),
        entry.problemDocId,
        entry.uid,
    );
    return { status: 'resolved', rid };
}

class OrchestratorSubmissionStatusHandler extends Handler {
    noCheckPermView = true;

    async post() {
        const headerToken = this.request.headers['x-orchestrator-token'];
        if (!tokenMatches(headerToken)) throw new InvalidTokenError('orchestrator');
        const submissionId = String(this.request.body?.submission_id || '');
        if (!/^[0-9a-f]{64}$/.test(submissionId)) {
            throw new BadRequestError('orchestrator submission status');
        }
        const entry = idempotencyState.entries[submissionId];
        if (!entry) {
            this.response.body = { status: 'unknown' };
            return;
        }
        const active = submissionResolutionInFlight.get(submissionId);
        const work = active || resolveAmbiguousSubmission(submissionId, entry);
        if (!active) submissionResolutionInFlight.set(submissionId, work);
        try {
            this.response.body = await work;
        } finally {
            if (!active) submissionResolutionInFlight.delete(submissionId);
        }
    }
}

function tokenMatches(input) {
    if (!TOKEN || typeof input !== 'string') return false;
    const expected = Buffer.from(TOKEN);
    const actual = Buffer.from(input);
    return expected.length === actual.length && crypto.timingSafeEqual(expected, actual);
}

function notificationText(body, key, maximumBytes, { trim = true, optional = false } = {}) {
    if (typeof body[key] !== 'string') {
        if (optional && body[key] === undefined) return '';
        throw new BadRequestError(`orchestrator notification ${key}`);
    }
    const value = trim ? body[key].trim() : body[key];
    if ((!optional && !value)
        || Buffer.byteLength(value, 'utf8') > maximumBytes
        || /[\u0000-\u001f\u007f]/.test(value)) {
        throw new BadRequestError(`orchestrator notification ${key}`);
    }
    return value;
}

function normalizeNotificationUrl(value) {
    let target;
    try {
        target = new URL(value);
    } catch {
        throw new BadRequestError('orchestrator notification desktop_url');
    }
    const hostname = target.hostname.toLowerCase().replace(/\.$/, '');
    if (target.protocol !== 'https:'
        || target.username
        || target.password
        || target.port
        || target.hash
        || !NOTIFICATION_ALLOWED_HOSTS.has(hostname)) {
        throw new BadRequestError('orchestrator notification desktop_url');
    }
    return target.toString();
}

function notificationFingerprint(payload) {
    return crypto.createHash('sha256')
        .update(JSON.stringify([
            payload.purpose,
            payload.uid,
            payload.contestTitle,
            payload.desktopUrl,
            payload.candidate,
            payload.studentPassword,
            payload.availableAt,
        ]))
        .digest('hex');
}

function notificationContent(payload, notificationId) {
    return JSON.stringify({
        message: SEAT_READY_MESSAGE,
        params: {
            contest: payload.contestTitle,
            available_at: payload.availableAt || '收到本消息后即可登录',
            desktop: payload.desktopUrl,
            candidate: payload.candidate,
            password: payload.studentPassword,
            // The Hydro renderer ignores unreferenced params. Keeping the
            // stable id inside the native message makes a retry recoverable
            // even if Hydro stopped immediately after inserting the message.
            _notification_id: notificationId,
        },
    });
}

function problemDraftSha256(value) {
    return crypto.createHash('sha256').update(value).digest('hex');
}

function problemDraftJsonSha256(value) {
    return problemDraftSha256(JSON.stringify(value));
}

function sameNumberArray(left, right) {
    return Array.isArray(left)
        && Array.isArray(right)
        && left.length === right.length
        && left.every((value, index) => value === right[index]);
}

function sameJsonValue(left, right) {
    return JSON.stringify(left) === JSON.stringify(right);
}

function parseMaterialPublicationRequest(body) {
    if (!body || Array.isArray(body) || typeof body !== 'object'
        || Object.keys(body).some((key) => !MATERIAL_FIELDS.has(key))) {
        throw new BadRequestError('orchestrator material publication payload');
    }
    const publicationId = String(body.publication_id || '').toLowerCase();
    const tid = String(body.tid || '').toLowerCase();
    const revision = String(body.revision || '');
    if (!/^[0-9a-f]{64}$/.test(publicationId)
        || !/^[0-9a-f]{24}$/.test(tid)
        || !/^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$/.test(revision)
        || !Array.isArray(body.attachments)
        || body.attachments.length !== MATERIAL_ATTACHMENT_NAMES.size) {
        throw new BadRequestError('orchestrator material publication identity');
    }
    let totalBytes = 0;
    const names = new Set();
    const attachments = body.attachments.map((raw) => {
        if (!raw || Array.isArray(raw) || typeof raw !== 'object'
            || Object.keys(raw).some((key) => !MATERIAL_ATTACHMENT_FIELDS.has(key))) {
            throw new BadRequestError('orchestrator material attachment');
        }
        const name = String(raw.name || '');
        const sha256 = String(raw.sha256 || '').toLowerCase();
        const encoded = String(raw.content_base64 || '');
        if (!MATERIAL_ATTACHMENT_NAMES.has(name)
            || names.has(name)
            || !/^[0-9a-f]{64}$/.test(sha256)
            || !encoded
            || encoded.length % 4 !== 0
            || !/^[A-Za-z0-9+/]*={0,2}$/.test(encoded)) {
            throw new BadRequestError('orchestrator material attachment');
        }
        const content = Buffer.from(encoded, 'base64');
        if (!content.length
            || content.toString('base64') !== encoded
            || problemDraftSha256(content) !== sha256) {
            throw new BadRequestError('orchestrator material attachment digest');
        }
        totalBytes += content.length;
        if (totalBytes > MATERIAL_MAX_BYTES) throw new OrchestratorMaterialTooLargeError();
        names.add(name);
        return { name, sha256, size: content.length, content };
    }).sort((left, right) => left.name.localeCompare(right.name));
    if ([...MATERIAL_ATTACHMENT_NAMES].some((name) => !names.has(name))) {
        throw new BadRequestError('orchestrator material attachment set');
    }
    return {
        publicationId,
        tid,
        revision,
        attachments,
    };
}

function materialPublicationFingerprint(request) {
    return problemDraftJsonSha256([
        'noi-material-publication-v1',
        request.tid,
        request.revision,
        request.attachments.map(({ name, sha256, size }) => ({ name, sha256, size })),
    ]);
}

async function exactStreamSha256(source, byteLimit) {
    const digest = crypto.createHash('sha256');
    let size = 0;
    for await (const value of source) {
        const chunk = Buffer.isBuffer(value) ? value : Buffer.from(value);
        size += chunk.length;
        if (size > byteLimit) throw new OrchestratorMaterialTooLargeError();
        digest.update(chunk);
    }
    return { sha256: digest.digest('hex'), size };
}

function materialStoragePath(tid, name) {
    return `contest/${DOMAIN}/${tid}/private/${name}`;
}

function materialMarker(request, fingerprint) {
    return {
        version: 1,
        publicationId: request.publicationId,
        revision: request.revision,
        fingerprint,
        attachments: request.attachments.map(({ name, sha256, size }) => ({
            name,
            sha256,
            size,
        })),
    };
}

function materialResult(entry) {
    return {
        status: 'published',
        publication_id: entry.publicationId,
        tid: entry.tid,
        revision: entry.revision,
        attachments: entry.attachments.map((item) => ({ ...item })),
    };
}

async function verifyPublishedMaterials(tdoc, entry) {
    const marker = tdoc.orchestratorMaterials;
    if (!marker
        || marker.version !== 1
        || marker.publicationId !== entry.publicationId
        || marker.revision !== entry.revision
        || marker.fingerprint !== entry.fingerprint
        || !sameJsonValue(marker.attachments, entry.attachments)) {
        return false;
    }
    const privateFiles = Array.isArray(tdoc.privateFiles) ? tdoc.privateFiles : [];
    for (const expected of entry.attachments) {
        const matches = privateFiles.filter((item) => item?.name === expected.name);
        if (matches.length !== 1 || Number(matches[0].size) !== expected.size) return false;
        const source = await StorageModel.get(materialStoragePath(entry.tid, expected.name));
        const actual = await exactStreamSha256(source, MATERIAL_MAX_BYTES);
        if (actual.size !== expected.size || actual.sha256 !== expected.sha256) return false;
    }
    return true;
}

function isOwnedMaterialMarker(marker) {
    if (!marker || marker.version !== 1
        || !/^[0-9a-f]{64}$/.test(String(marker.publicationId || ''))
        || !/^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$/.test(String(marker.revision || ''))
        || !/^[0-9a-f]{64}$/.test(String(marker.fingerprint || ''))
        || !Array.isArray(marker.attachments)
        || marker.attachments.length !== MATERIAL_ATTACHMENT_NAMES.size) {
        return false;
    }
    const names = new Set();
    for (const item of marker.attachments) {
        if (!item || typeof item !== 'object' || Array.isArray(item)
            || !MATERIAL_ATTACHMENT_NAMES.has(item.name)
            || names.has(item.name)
            || !/^[0-9a-f]{64}$/.test(String(item.sha256 || ''))
            || !Number.isSafeInteger(item.size)
            || item.size <= 0
            || item.size > MATERIAL_MAX_BYTES) {
            return false;
        }
        names.add(item.name);
    }
    return [...MATERIAL_ATTACHMENT_NAMES].every((name) => names.has(name));
}

function snapshotPidRecord(value, pids, field) {
    if (value === undefined) return undefined;
    if (!value || Array.isArray(value) || typeof value !== 'object') {
        throw new OrchestratorProblemDraftBlockedError(`invalid contest ${field}`);
    }
    const allowed = new Set(pids.map(String));
    if (Object.keys(value).some((key) => !/^[1-9][0-9]*$/.test(key) || !allowed.has(key))) {
        throw new OrchestratorProblemDraftBlockedError(`stale contest ${field}`);
    }
    let serialized;
    try {
        serialized = JSON.stringify(value);
    } catch {
        throw new OrchestratorProblemDraftBlockedError(`invalid contest ${field}`);
    }
    if (Buffer.byteLength(serialized, 'utf8') > 1024 * 1024) {
        throw new OrchestratorProblemDraftTooLargeError();
    }
    return JSON.parse(serialized);
}

function remapPidRecord(value, sourcePids, targetPids) {
    if (value === undefined) return undefined;
    const result = {};
    sourcePids.forEach((sourcePid, index) => {
        if (Object.hasOwn(value, String(sourcePid))) {
            result[String(targetPids[index])] = value[String(sourcePid)];
        }
    });
    return result;
}

function safeProblemDataName(value) {
    return typeof value === 'string'
        && value.length > 0
        && Buffer.byteLength(value, 'utf8') <= 512
        && !value.startsWith('/')
        && !value.startsWith('\\')
        && !value.includes('\\')
        && !value.includes('//')
        && !value.split('/').includes('..')
        && !/[\u0000-\u001f\u007f]/.test(value);
}

function parseProblemDraftRequest(body) {
    if (!body || Array.isArray(body) || typeof body !== 'object'
        || Object.keys(body).some((key) => !PROBLEM_DRAFT_FIELDS.has(key))) {
        throw new BadRequestError('orchestrator problem draft payload');
    }
    if (typeof body.action !== 'string' || typeof body.tid !== 'string') {
        throw new BadRequestError('orchestrator problem draft payload');
    }
    const action = body.action;
    const tid = body.tid.toLowerCase();
    if (!['preflight', 'apply'].includes(action) || !ObjectId.isValid(tid)) {
        throw new BadRequestError('orchestrator problem draft payload');
    }
    if (!Array.isArray(body.problems)
        || body.problems.length < 1
        || body.problems.length > PROBLEM_DRAFT_MAX_PROBLEMS) {
        throw new BadRequestError('orchestrator problem draft problems');
    }
    const pids = new Set();
    const slugs = new Set();
    const problems = body.problems.map((item) => {
        if (!item || Array.isArray(item) || typeof item !== 'object'
            || Object.keys(item).some((key) => !PROBLEM_DRAFT_PROBLEM_FIELDS.has(key))) {
            throw new BadRequestError('orchestrator problem draft problem');
        }
        if (typeof item.pid !== 'string' || typeof item.slug !== 'string') {
            throw new BadRequestError('orchestrator problem draft problem');
        }
        const pid = item.pid;
        const slug = item.slug;
        const validPid = /^(?:(?:[a-z0-9]{1,10}-)?[a-z][a-z0-9]*|[1-9][0-9]{0,9})$/i.test(pid)
            && pid.length <= 64;
        if (!validPid || !/^[a-z][a-z0-9_]{0,63}$/.test(slug)) {
            throw new BadRequestError('orchestrator problem draft problem');
        }
        if (pids.has(pid.toLowerCase()) || slugs.has(slug)) {
            throw new BadRequestError('orchestrator problem draft duplicate');
        }
        pids.add(pid.toLowerCase());
        slugs.add(slug);
        return { pid, slug };
    });
    const operationId = body.operation_id;
    const approvalId = body.approval_id;
    const preflightId = body.preflight_id;
    if (action === 'preflight') {
        if (Object.hasOwn(body, 'operation_id')
            || Object.hasOwn(body, 'approval_id')
            || Object.hasOwn(body, 'preflight_id')) {
            throw new BadRequestError('orchestrator problem draft preflight payload');
        }
    } else if (![operationId, approvalId, preflightId]
        .every((value) => typeof value === 'string' && /^[0-9a-f]{64}$/.test(value))) {
        throw new BadRequestError('orchestrator problem draft approval');
    }
    return {
        action,
        tid,
        problems,
        operationId,
        approvalId,
        preflightId,
    };
}

let problemDraftYaml;
function loadProblemDraftYaml() {
    // js-yaml is a declared Hydro 5.0.1 dependency. Loading it lazily keeps
    // the rest of this addon usable even when this optional route is not used.
    // The production require hook resolves dependencies from Hydro itself.
    if (problemDraftYaml) return problemDraftYaml;
    try {
        // eslint-disable-next-line global-require
        problemDraftYaml = require('js-yaml');
    } catch {
        // pnpm/Nix can isolate transitive packages from an addon directory;
        // resolve the declared dependency relative to Hydro's public entrypoint.
        // eslint-disable-next-line global-require
        const { createRequire } = require('module');
        problemDraftYaml = createRequire(require.resolve('hydrooj'))('js-yaml');
    }
    return problemDraftYaml;
}

function parseRawProblemConfig(rawConfig) {
    if (typeof rawConfig !== 'string'
        || Buffer.byteLength(rawConfig, 'utf8') > 4 * 1024 * 1024
        || /\u0000/.test(rawConfig)) {
        throw new OrchestratorProblemDraftBlockedError('invalid raw config');
    }
    let parsed;
    try {
        parsed = rawConfig.trim() ? loadProblemDraftYaml().load(rawConfig) : {};
    } catch {
        throw new OrchestratorProblemDraftBlockedError('unparseable raw config');
    }
    if (parsed === null) parsed = {};
    if (Array.isArray(parsed) || typeof parsed !== 'object') {
        throw new OrchestratorProblemDraftBlockedError('config root must be a mapping');
    }
    return parsed;
}

function fileIoConfig(rawConfig, slug) {
    const config = parseRawProblemConfig(rawConfig);
    if (config.type && config.type !== 'default') {
        throw new OrchestratorProblemDraftBlockedError('unsupported problem type');
    }
    config.filename = slug;
    try {
        return loadProblemDraftYaml().dump(config, {
            noRefs: true,
            lineWidth: 120,
            noCompatMode: true,
        });
    } catch {
        throw new OrchestratorProblemDraftBlockedError('config cannot be serialized safely');
    }
}

function fileIoNotice(slug, html) {
    if (html) {
        return `<h2>本场文件读写要求</h2><p>程序从 <code>${slug}.in</code> 读取输入，并将答案写入 <code>${slug}.out</code>。</p>`;
    }
    return `## 本场文件读写要求\n\n程序从 \`${slug}.in\` 读取输入，并将答案写入 \`${slug}.out\`。`;
}

function appendFileIoNotice(content, slug, html) {
    if (typeof content !== 'string'
        || Buffer.byteLength(content, 'utf8') > 8 * 1024 * 1024
        || /\u0000/.test(content)) {
        throw new OrchestratorProblemDraftBlockedError('invalid problem content');
    }
    const notice = fileIoNotice(slug, html);
    try {
        const multilingual = JSON.parse(content);
        if (multilingual && !Array.isArray(multilingual) && typeof multilingual === 'object') {
            const entries = Object.entries(multilingual);
            if (entries.length && entries.every(([, value]) => typeof value === 'string')) {
                return JSON.stringify(Object.fromEntries(
                    entries.map(([key, value]) => [key, `${value}\n\n${notice}`]),
                ));
            }
        }
    } catch {
        // Ordinary Markdown/HTML content is not JSON and is handled below.
    }
    return `${content}\n\n${notice}`;
}

function collectFormalInputNames(config, files) {
    const names = new Set(files.filter((name) => /\.in$/i.test(name)));
    if (config.answers && !Array.isArray(config.answers) && typeof config.answers === 'object') {
        for (const name of Object.keys(config.answers)) names.add(name);
    }
    const addCases = (cases) => {
        if (!Array.isArray(cases)) return;
        for (const item of cases) {
            if (item && typeof item === 'object' && typeof item.input === 'string') {
                names.add(item.input);
            }
        }
    };
    addCases(config.cases);
    if (Array.isArray(config.subtasks)) {
        for (const subtask of config.subtasks) addCases(subtask?.cases);
    }
    return [...names].filter((name) => files.includes(name)).sort();
}

function inputWhitespace(byte) {
    return byte === 0x09
        || byte === 0x0a
        || byte === 0x0b
        || byte === 0x0c
        || byte === 0x0d
        || byte === 0x20;
}

async function normalizedInputSha256(source, byteBudget) {
    // This intentionally matches artifact_generation.normalized_input_digest:
    // CRLF/CR -> LF, bytes.rstrip() per line, then bytes.strip() globally.
    const hash = crypto.createHash('sha256');
    const iterable = Buffer.isBuffer(source) || typeof source === 'string'
        ? [source]
        : source;
    const lineParts = [];
    let lineBytes = 0;
    let lineIndex = 0;
    let outputStarted = false;
    let pendingSeparators = 0;
    let total = 0;
    let skipLeadingLineFeed = false;

    const hashSeparators = () => {
        const block = Buffer.alloc(8192, 0x0a);
        while (pendingSeparators > 0) {
            const count = Math.min(pendingSeparators, block.length);
            hash.update(block.subarray(0, count));
            pendingSeparators -= count;
        }
    };
    const finishLine = () => {
        if (lineIndex > 0) pendingSeparators++;
        const line = lineParts.length === 1
            ? lineParts[0]
            : Buffer.concat(lineParts, lineBytes);
        let end = line.length;
        while (end > 0 && inputWhitespace(line[end - 1])) end--;
        let start = 0;
        if (!outputStarted) {
            while (start < end && inputWhitespace(line[start])) start++;
        }
        if (start < end) {
            if (outputStarted) hashSeparators();
            else pendingSeparators = 0;
            hash.update(line.subarray(start, end));
            outputStarted = true;
        }
        lineParts.length = 0;
        lineBytes = 0;
        lineIndex++;
    };
    const appendLinePart = (part) => {
        if (!part.length) return;
        lineBytes += part.length;
        if (lineBytes > PROBLEM_DRAFT_MAX_LINE_BYTES) {
            throw new OrchestratorProblemDraftTooLargeError();
        }
        lineParts.push(part);
    };
    const processNormalized = (normalized) => {
        let start = 0;
        for (let index = 0; index < normalized.length; index++) {
            if (normalized[index] !== 0x0a) continue;
            appendLinePart(normalized.subarray(start, index));
            finishLine();
            start = index + 1;
        }
        appendLinePart(normalized.subarray(start));
    };

    for await (const value of iterable) {
        const chunk = Buffer.isBuffer(value) ? value : Buffer.from(value);
        total += chunk.length;
        if (total > byteBudget) throw new OrchestratorProblemDraftTooLargeError();
        const normalized = Buffer.allocUnsafe(chunk.length);
        let output = 0;
        let index = 0;
        if (skipLeadingLineFeed && chunk.length) {
            if (chunk[0] === 0x0a) index = 1;
            skipLeadingLineFeed = false;
        }
        for (; index < chunk.length; index++) {
            if (chunk[index] !== 0x0d) {
                normalized[output++] = chunk[index];
                continue;
            }
            normalized[output++] = 0x0a;
            if (index + 1 < chunk.length) {
                if (chunk[index + 1] === 0x0a) index++;
            } else {
                skipLeadingLineFeed = true;
            }
        }
        if (output) processNormalized(normalized.subarray(0, output));
    }
    finishLine();
    return { sha256: hash.digest('hex'), bytes: total };
}

function normalizedProblemFileList(value) {
    if (!Array.isArray(value) || value.length > PROBLEM_DRAFT_MAX_FILES) {
        throw new OrchestratorProblemDraftTooLargeError();
    }
    return value.map((item) => {
        const name = String(item?.name || item?._id || '');
        if (!safeProblemDataName(name)) {
            throw new OrchestratorProblemDraftBlockedError('unsafe problem data path');
        }
        const size = Number(item?.size || 0);
        if (!Number.isSafeInteger(size) || size < 0) {
            throw new OrchestratorProblemDraftBlockedError('invalid problem data size');
        }
        return {
            name,
            size,
            etag: String(item?.etag || ''),
        };
    }).sort((left, right) => left.name.localeCompare(right.name));
}

async function formalInputHashes(pdoc, parsedRawConfig) {
    const data = normalizedProblemFileList(pdoc.data || []);
    const names = collectFormalInputNames(parsedRawConfig, data.map((item) => item.name));
    const selected = names.map((name) => data.find((item) => item.name === name));
    const declaredBytes = selected.reduce((sum, item) => sum + item.size, 0);
    if (declaredBytes > PROBLEM_DRAFT_MAX_HASH_BYTES) {
        throw new OrchestratorProblemDraftTooLargeError();
    }
    let remaining = PROBLEM_DRAFT_MAX_HASH_BYTES;
    const hashes = [];
    for (const item of selected) {
        const stream = await StorageModel.get(
            `problem/${DOMAIN}/${pdoc.docId}/testdata/${item.name}`,
        );
        const result = await normalizedInputSha256(stream, remaining);
        remaining -= result.bytes;
        hashes.push(result.sha256);
    }
    return [...new Set(hashes)].sort();
}

function publicConfigSummary(parsedConfig, rawConfig) {
    const raw = parseRawProblemConfig(rawConfig);
    return {
        type: String(parsedConfig.type || 'default'),
        count: Number(parsedConfig.count || 0),
        sub_type: String(parsedConfig.subType || ''),
        filename: typeof raw.filename === 'string' ? raw.filename : '',
        hackable: Boolean(parsedConfig.hackable),
        langs: Array.isArray(parsedConfig.langs)
            ? parsedConfig.langs.map(String).slice(0, 100)
            : [],
    };
}

function sourceDescriptor(pdoc, formalHashes) {
    const data = normalizedProblemFileList(pdoc.data || []);
    const additional = normalizedProblemFileList(pdoc.additional_file || []);
    return {
        docId: pdoc.docId,
        pid: String(pdoc.pid || ''),
        title: String(pdoc.title || ''),
        contentSha256: problemDraftSha256(String(pdoc.content || '')),
        configSha256: problemDraftSha256(String(pdoc.config || '')),
        owner: Number(pdoc.owner || 0),
        difficulty: Number(pdoc.difficulty || 0),
        tag: Array.isArray(pdoc.tag) ? pdoc.tag.map(String).sort() : [],
        html: Boolean(pdoc.html),
        hidden: Boolean(pdoc.hidden),
        data,
        additional,
        formalHashes,
    };
}

async function contestProblemDraftBlockers(tdoc) {
    const blockers = [];
    if (tdoc.rule !== 'oi') blockers.push('contest_rule_is_not_oi');
    const beginAt = new Date(tdoc.beginAt).getTime();
    if (!Number.isFinite(beginAt) || Date.now() >= beginAt) blockers.push('contest_already_started');
    const submissionCount = await RecordModel.coll.countDocuments({
        domainId: DOMAIN,
        contest: tdoc.docId,
    }, { limit: 1 });
    if (submissionCount > 0) blockers.push('contest_has_submissions');
    const journalCount = await DocumentModel.collStatus.countDocuments({
        domainId: DOMAIN,
        docType: DocumentModel.TYPE_CONTEST,
        docId: tdoc.docId,
        'journal.0': { $exists: true },
    }, { limit: 1 });
    if (journalCount > 0) blockers.push('contest_has_submission_journal');
    const clarificationCount = await DocumentModel.coll.countDocuments({
        domainId: DOMAIN,
        docType: DocumentModel.TYPE_CONTEST_CLARIFICATION,
        parentType: DocumentModel.TYPE_CONTEST,
        parentId: tdoc.docId,
        subject: { $in: tdoc.pids },
    }, { limit: 1 });
    if (clarificationCount > 0) blockers.push('contest_has_problem_clarifications');
    if (tdoc.orchestratorFileIo?.operationId) blockers.push('file_io_clone_already_applied');
    return blockers;
}

async function resolveContestProblemDraft(tdoc, requested) {
    if (!Array.isArray(tdoc.pids)
        || tdoc.pids.length < 1
        || tdoc.pids.length !== requested.length) {
        throw new OrchestratorProblemDraftBlockedError('problem list must cover the contest exactly');
    }
    const resolved = [];
    const usedDocIds = new Set();
    for (const item of requested) {
        const raw = await ProblemModel.get(
            DOMAIN,
            item.pid,
            [...ProblemModel.PROJECTION_PUBLIC, 'orchestratorFileIoClone'],
            true,
        );
        if (!raw || !tdoc.pids.includes(raw.docId) || usedDocIds.has(raw.docId)) {
            throw new BadRequestError('orchestrator problem draft pid');
        }
        if (raw.reference || raw.orchestratorFileIoClone) {
            throw new OrchestratorProblemDraftBlockedError('referenced or cloned source problem');
        }
        const parsed = await ProblemModel.get(DOMAIN, raw.docId);
        if (!parsed || typeof parsed.config !== 'object' || parsed.config.type !== 'default') {
            throw new OrchestratorProblemDraftBlockedError('unsupported or invalid problem config');
        }
        const parsedRawConfig = parseRawProblemConfig(raw.config);
        const hashes = await formalInputHashes(raw, parsedRawConfig);
        const descriptor = sourceDescriptor(raw, hashes);
        usedDocIds.add(raw.docId);
        resolved.push({
            requestPid: item.pid,
            slug: item.slug,
            raw,
            parsed,
            parsedRawConfig,
            hashes,
            descriptor,
        });
    }
    if (tdoc.pids.some((docId) => !usedDocIds.has(docId))) {
        throw new OrchestratorProblemDraftBlockedError('problem list must cover the contest exactly');
    }
    const byDocId = new Map(resolved.map((item) => [item.raw.docId, item]));
    return tdoc.pids.map((docId) => byDocId.get(docId));
}

async function buildProblemDraftPreflight(request) {
    const tid = new ObjectId(request.tid);
    const tdoc = await ContestModel.get(DOMAIN, tid);
    const blockers = await contestProblemDraftBlockers(tdoc);
    if (blockers.length) {
        return {
            safe_to_apply: false,
            blockers,
            tid: request.tid,
            preflight_id: '',
            problems: [],
            _tdoc: tdoc,
            _resolved: [],
        };
    }
    const resolved = await resolveContestProblemDraft(tdoc, request.problems);
    const score = snapshotPidRecord(tdoc.score, tdoc.pids, 'score');
    const balloon = snapshotPidRecord(tdoc.balloon, tdoc.pids, 'balloon');
    const contestSnapshot = {
        tid: request.tid,
        title: String(tdoc.title || ''),
        contentSha256: problemDraftSha256(String(tdoc.content || '')),
        rule: tdoc.rule,
        beginAt: new Date(tdoc.beginAt).toISOString(),
        endAt: new Date(tdoc.endAt).toISOString(),
        duration: Number(tdoc.duration || 0),
        langs: Array.isArray(tdoc.langs) ? tdoc.langs.map(String).sort() : [],
        pids: [...tdoc.pids],
        score,
        balloon,
        problems: resolved.map((item) => ({
            slug: item.slug,
            source: item.descriptor,
        })),
    };
    const preflightId = problemDraftJsonSha256(['noi-fileio-preflight-v1', contestSnapshot]);
    return {
        safe_to_apply: true,
        blockers: [],
        tid: request.tid,
        preflight_id: preflightId,
        contest_title: String(tdoc.title || ''),
        begin_at: contestSnapshot.beginAt,
        end_at: contestSnapshot.endAt,
        problems: resolved.map((item) => ({
            pid: String(item.raw.pid || item.raw.docId),
            doc_id: item.raw.docId,
            slug: item.slug,
            title: String(item.raw.title || ''),
            content: String(item.raw.content || ''),
            config: publicConfigSummary(item.parsed.config, item.raw.config),
            time_ms: {
                min: Number(item.parsed.config.timeMin || 0),
                max: Number(item.parsed.config.timeMax || 0),
            },
            memory_mb: {
                min: Number(item.parsed.config.memoryMin || 0),
                max: Number(item.parsed.config.memoryMax || 0),
            },
            formal_input_sha256: item.hashes,
            source_hash: problemDraftJsonSha256(item.descriptor),
        })),
        _tdoc: tdoc,
        _resolved: resolved,
        _score: score,
        _balloon: balloon,
    };
}

function publicProblemDraftPreflight(preflight) {
    const result = { ...preflight };
    delete result._tdoc;
    delete result._resolved;
    delete result._score;
    delete result._balloon;
    return result;
}

function clonePidFor(operationId, tid, slug) {
    return `noi${tid.slice(0, 6)}-p${problemDraftSha256(slug).slice(0, 8)}${operationId.slice(0, 6)}`;
}

function cloneMarkerFor(operationId) {
    return `__noi_fileio_${operationId.slice(0, 16)}`;
}

async function ensureProblemDraftCloneDocument(item, tdoc, request) {
    const clonePid = clonePidFor(request.operationId, request.tid, item.slug);
    const marker = cloneMarkerFor(request.operationId);
    const content = appendFileIoNotice(item.raw.content, item.slug, Boolean(item.raw.html));
    let clone = await ProblemModel.get(
        DOMAIN,
        clonePid,
        [...ProblemModel.PROJECTION_PUBLIC, 'orchestratorFileIoClone'],
        true,
    );
    if (clone) {
        const cloneRecords = await RecordModel.coll.countDocuments({
            domainId: DOMAIN,
            pid: clone.docId,
        }, { limit: 1 });
        if (cloneRecords > 0
            || !Array.isArray(clone.tag)
            || !clone.tag.includes(marker)
            || clone.title !== item.raw.title
            || clone.content !== content
            || !clone.hidden
            || (clone.orchestratorFileIoClone
                && (clone.orchestratorFileIoClone.operationId !== request.operationId
                    || clone.orchestratorFileIoClone.preflightId !== request.preflightId
                    || clone.orchestratorFileIoClone.sourceDocId !== item.raw.docId
                    || clone.orchestratorFileIoClone.slug !== item.slug))) {
            throw new OrchestratorProblemDraftConflictError();
        }
        return { docId: clone.docId, pid: clonePid, marker, content };
    }
    const tag = [...new Set([...(Array.isArray(item.raw.tag) ? item.raw.tag : []), marker])];
    const docId = await ProblemModel.add(
        DOMAIN,
        clonePid,
        item.raw.title,
        content,
        Number(tdoc.owner || 1),
        tag,
        {
            hidden: true,
            ...(typeof item.raw.difficulty === 'number'
                ? { difficulty: item.raw.difficulty }
                : {}),
        },
    );
    return { docId, pid: clonePid, marker, content };
}

function problemDraftCloneVerificationMismatches(raw, parsed, expected) {
    const mismatches = [];
    if (!raw) mismatches.push('raw_missing');
    if (!parsed) mismatches.push('parsed_missing');
    if (!raw || !parsed) return mismatches;

    const actualData = normalizedProblemFileList(raw.data || []);
    const actualCopiedData = actualData
        .filter((file) => file.name !== 'config.yaml')
        .map((file) => ({ name: file.name, size: file.size }));
    const actualConfigNames = actualData
        .filter((file) => CONFIG_FILENAMES.has(file.name))
        .map((file) => file.name);
    const actualAdditional = normalizedProblemFileList(raw.additional_file || [])
        .map((file) => ({ name: file.name, size: file.size }));

    if (raw.pid !== expected.pid) mismatches.push('pid');
    if (raw.content !== expected.content) mismatches.push('content');
    if (!raw.hidden) mismatches.push('hidden');
    if (raw.reference) mismatches.push('reference');
    if (raw.orchestratorFileIoClone?.operationId !== expected.operationId) {
        mismatches.push('operation_marker');
    }
    if (parsed.config?.type !== 'default') mismatches.push('config_type');
    if (parsed.config?.subType !== expected.slug) mismatches.push('config_filename');
    if (JSON.stringify(expected.copiedData) !== JSON.stringify(actualCopiedData)) {
        mismatches.push('testdata');
    }
    if (JSON.stringify(actualConfigNames) !== JSON.stringify(['config.yaml'])) {
        mismatches.push('config_file');
    }
    if (JSON.stringify(expected.additional) !== JSON.stringify(actualAdditional)) {
        mismatches.push('additional_files');
    }
    return mismatches;
}

function waitForProblemDraftCloneVerification(attempt) {
    const delay = Math.min(
        PROBLEM_DRAFT_CLONE_VERIFY_BASE_DELAY_MS * (2 ** (attempt - 1)),
        PROBLEM_DRAFT_CLONE_VERIFY_MAX_DELAY_MS,
    );
    return new Promise((resolve) => {
        setTimeout(resolve, delay);
    });
}

async function verifyProblemDraftClone(item, clone, request, expected) {
    let mismatches = [];
    // Hydro 5.0.x can briefly return its cached pre-edit problem snapshot
    // immediately after addTestdata/edit. Re-read within a small fixed bound;
    // no mismatch is waived and the contest CAS still waits for a clean read.
    for (let attempt = 1; attempt <= PROBLEM_DRAFT_CLONE_VERIFY_ATTEMPTS; attempt++) {
        const raw = await ProblemModel.get(
            DOMAIN,
            clone.docId,
            [...ProblemModel.PROJECTION_PUBLIC, 'orchestratorFileIoClone'],
            true,
        );
        const parsed = await ProblemModel.get(DOMAIN, clone.docId);
        mismatches = problemDraftCloneVerificationMismatches(raw, parsed, {
            ...expected,
            pid: clone.pid,
            content: clone.content,
            operationId: request.operationId,
            slug: item.slug,
        });
        if (!mismatches.length) return;
        if (attempt < PROBLEM_DRAFT_CLONE_VERIFY_ATTEMPTS) {
            await waitForProblemDraftCloneVerification(attempt);
        }
    }
    // Never print request data, problem metadata, filenames, or expected/actual
    // values. These fixed labels are enough to diagnose an eventual-consistency
    // miss without exposing a private statement or test-data detail.
    console.error(
        `[noi-fileio] clone verification failed mismatches=${mismatches.join(',')}`,
    );
    throw new OrchestratorProblemDraftConflictError();
}

async function populateAndVerifyProblemDraftClone(item, clone, request) {
    const sourceData = normalizedProblemFileList(item.raw.data || []);
    const sourceAdditional = normalizedProblemFileList(item.raw.additional_file || []);
    for (const file of sourceData) {
        if (CONFIG_FILENAMES.has(file.name)) continue;
        const stream = await StorageModel.get(
            `problem/${DOMAIN}/${item.raw.docId}/testdata/${file.name}`,
        );
        await ProblemModel.addTestdata(DOMAIN, clone.docId, file.name, stream, 1);
    }
    const config = fileIoConfig(item.raw.config, item.slug);
    await ProblemModel.addTestdata(
        DOMAIN,
        clone.docId,
        'config.yaml',
        Buffer.from(config, 'utf8'),
        1,
    );
    for (const file of sourceAdditional) {
        const stream = await StorageModel.get(
            `problem/${DOMAIN}/${item.raw.docId}/additional_file/${file.name}`,
        );
        await ProblemModel.addAdditionalFile(DOMAIN, clone.docId, file.name, stream, 1);
    }
    await ProblemModel.edit(DOMAIN, clone.docId, {
        html: Boolean(item.raw.html),
        hidden: true,
        orchestratorFileIoClone: {
            version: 1,
            operationId: request.operationId,
            preflightId: request.preflightId,
            approvalId: request.approvalId,
            tid: request.tid,
            sourceDocId: item.raw.docId,
            slug: item.slug,
        },
    });
    const expectedCopiedData = sourceData
        .filter((file) => !CONFIG_FILENAMES.has(file.name))
        .map((file) => ({ name: file.name, size: file.size }));
    const expectedAdditional = sourceAdditional
        .map((file) => ({ name: file.name, size: file.size }));
    await verifyProblemDraftClone(item, clone, request, {
        copiedData: expectedCopiedData,
        additional: expectedAdditional,
    });
}

function problemDraftResult(entry) {
    return {
        status: 'applied',
        operation_id: entry.operationId,
        preflight_id: entry.preflightId,
        tid: entry.tid,
        pids: [...entry.targetPids],
        mapping: entry.mapping.map((item) => ({ ...item })),
    };
}

function acquireContestSubmissionLease(tid) {
    if (contestDraftMutations.has(tid) || contestMaterialMutations.has(tid)) {
        throw new OrchestratorProblemDraftConflictError();
    }
    contestSubmissionLeases.set(tid, (contestSubmissionLeases.get(tid) || 0) + 1);
}

function releaseContestSubmissionLease(tid) {
    const remaining = (contestSubmissionLeases.get(tid) || 1) - 1;
    if (remaining > 0) contestSubmissionLeases.set(tid, remaining);
    else contestSubmissionLeases.delete(tid);
}

function acquireContestDraftMutation(tid) {
    if (contestDraftMutations.has(tid)
        || contestMaterialMutations.has(tid)
        || contestSubmissionLeases.get(tid)) {
        throw new OrchestratorProblemDraftConflictError();
    }
    contestDraftMutations.add(tid);
}

function acquireContestMaterialMutation(tid) {
    if (contestDraftMutations.has(tid)
        || contestMaterialMutations.has(tid)
        || contestSubmissionLeases.get(tid)) {
        throw new OrchestratorMaterialConflictError();
    }
    contestMaterialMutations.add(tid);
}

function problemDraftFingerprint(request) {
    return problemDraftJsonSha256([
        'noi-fileio-apply-v1',
        request.tid,
        request.preflightId,
        request.approvalId,
        request.problems,
    ]);
}

function optionalContestFieldMatches(tdoc, entry, field) {
    const flag = `has${field[0].toUpperCase()}${field.slice(1)}`;
    const target = `target${field[0].toUpperCase()}${field.slice(1)}`;
    if (entry[flag]) return sameJsonValue(tdoc[field], entry[target]);
    return tdoc[field] === undefined;
}

function contestMatchesAppliedProblemDraft(tdoc, entry) {
    return entry.mapping?.length > 0
        && entry.mapping.every((item) => item.verified)
        && sameNumberArray(tdoc.pids, entry.targetPids)
        && tdoc.orchestratorFileIo?.operationId === entry.operationId
        && tdoc.orchestratorFileIo?.preflightId === entry.preflightId
        && optionalContestFieldMatches(tdoc, entry, 'score')
        && optionalContestFieldMatches(tdoc, entry, 'balloon');
}

function dateMs(value) {
    if (!value) return null;
    const result = new Date(value).getTime();
    return Number.isFinite(result) ? result : null;
}

function acceptedAtInContestWindow(tdoc, tsdoc, acceptedAtMs) {
    const globalBeginAtMs = dateMs(tdoc.beginAt);
    const globalEndAtMs = dateMs(tdoc.endAt);
    if (globalBeginAtMs === null || globalEndAtMs === null) return false;

    const personalStartAtMs = dateMs(tsdoc?.startAt);
    const beginAtMs = personalStartAtMs === null
        ? globalBeginAtMs
        : Math.max(globalBeginAtMs, personalStartAtMs);
    const deadlines = [globalEndAtMs];
    const personalEndAtMs = dateMs(tsdoc?.endAt);
    if (personalEndAtMs !== null) deadlines.push(personalEndAtMs);

    const durationHours = Number(tdoc.duration);
    if (personalStartAtMs !== null
        && Number.isFinite(durationHours)
        && durationHours > 0) {
        deadlines.push(personalStartAtMs + Math.floor(durationHours * 60 * 60 * 1000));
    }
    const endAtMs = Math.min(...deadlines);
    return acceptedAtMs >= beginAtMs && acceptedAtMs < endAtMs;
}

function pickCc14() {
    const langs = SettingModel.langs || {};
    const keys = Object.keys(langs).filter((key) => !langs[key]?.disabled);
    return keys.find((key) => key === 'cc.cc14o2')
        || keys.find((key) => key === 'cc.cc14')
        || keys.find((key) => key.startsWith('cc') && key.includes('14'))
        || keys.find((key) => key === 'cc')
        || 'cc';
}

class OrchestratorProblemFileIoHandler extends Handler {
    noCheckPermView = true;

    async post() {
        const headerToken = this.request.headers['x-orchestrator-token'];
        if (!tokenMatches(headerToken)) throw new InvalidTokenError('orchestrator');

        const request = parseProblemDraftRequest(this.request.body || {});
        if (request.action === 'preflight') {
            const preflight = await buildProblemDraftPreflight(request);
            this.response.body = publicProblemDraftPreflight(preflight);
            return;
        }

        const fingerprint = problemDraftFingerprint(request);
        let entry = problemDraftIdempotencyState.entries[request.operationId];
        if (entry && entry.fingerprint !== fingerprint) {
            throw new OrchestratorProblemDraftConflictError();
        }
        const active = problemDraftInFlight.get(request.operationId);
        if (active && active.fingerprint !== fingerprint) {
            throw new OrchestratorProblemDraftConflictError();
        }

        const validateComplete = async (completeEntry) => {
            const tdoc = await ContestModel.get(DOMAIN, new ObjectId(request.tid));
            if (!contestMatchesAppliedProblemDraft(tdoc, completeEntry)) {
                throw new OrchestratorProblemDraftConflictError();
            }
            return problemDraftResult(completeEntry);
        };

        if (entry?.complete) {
            this.response.body = await validateComplete(entry);
            return;
        }

        const work = active?.promise || (async () => {
            entry = problemDraftIdempotencyState.entries[request.operationId];
            if (!entry) {
                entry = {
                    version: 1,
                    operationId: request.operationId,
                    preflightId: request.preflightId,
                    approvalId: request.approvalId,
                    tid: request.tid,
                    fingerprint,
                    sourcePids: [],
                    targetPids: [],
                    mapping: [],
                    complete: false,
                    createdAt: new Date().toISOString(),
                };
                problemDraftIdempotencyState.entries[request.operationId] = entry;
                // Reserve the operation id before creating any Hydro document.
                saveProblemDraftIdempotencyState();
            }

            acquireContestDraftMutation(request.tid);
            try {
                if (entry.complete) return await validateComplete(entry);
                if (entry.targetPids?.length
                    && entry.mapping?.length
                    && entry.mapping.every((item) => item.verified)) {
                    const applied = await ContestModel.get(
                        DOMAIN,
                        new ObjectId(request.tid),
                    );
                    if (contestMatchesAppliedProblemDraft(applied, entry)) {
                        // Recovery for a crash after the atomic contest CAS and
                        // before the filesystem journal marked the operation complete.
                        entry.complete = true;
                        entry.completedAt ||= new Date().toISOString();
                        saveProblemDraftIdempotencyState();
                        return problemDraftResult(entry);
                    }
                    if (applied.orchestratorFileIo?.operationId === request.operationId) {
                        throw new OrchestratorProblemDraftConflictError();
                    }
                }
                const preflight = await buildProblemDraftPreflight(request);
                if (!preflight.safe_to_apply) {
                    throw new OrchestratorProblemDraftBlockedError(
                        preflight.blockers.join(','),
                    );
                }
                if (preflight.preflight_id !== request.preflightId) {
                    throw new OrchestratorProblemDraftConflictError();
                }
                const sourcePids = [...preflight._tdoc.pids];
                if (entry.sourcePids.length && !sameNumberArray(entry.sourcePids, sourcePids)) {
                    throw new OrchestratorProblemDraftConflictError();
                }
                entry.sourcePids = sourcePids;
                entry.hasScore = preflight._score !== undefined;
                entry.sourceScore = preflight._score;
                entry.hasBalloon = preflight._balloon !== undefined;
                entry.sourceBalloon = preflight._balloon;
                saveProblemDraftIdempotencyState();

                const mapping = [];
                for (const item of preflight._resolved) {
                    const clone = await ensureProblemDraftCloneDocument(
                        item,
                        preflight._tdoc,
                        request,
                    );
                    const saved = entry.mapping.find(
                        (value) => value.source_doc_id === item.raw.docId,
                    );
                    if (saved
                        && (saved.clone_doc_id !== clone.docId || saved.clone_pid !== clone.pid)) {
                        throw new OrchestratorProblemDraftConflictError();
                    }
                    const current = saved || {
                        source_pid: String(item.raw.pid || item.raw.docId),
                        source_doc_id: item.raw.docId,
                        clone_pid: clone.pid,
                        clone_doc_id: clone.docId,
                        slug: item.slug,
                        verified: false,
                    };
                    if (!saved) entry.mapping.push(current);
                    saveProblemDraftIdempotencyState();
                    await populateAndVerifyProblemDraftClone(item, clone, request);
                    current.verified = true;
                    saveProblemDraftIdempotencyState();
                    mapping.push(current);
                }
                if (mapping.some((item) => !item.verified)) {
                    throw new OrchestratorProblemDraftConflictError();
                }
                entry.mapping = mapping;
                entry.targetPids = mapping.map((item) => item.clone_doc_id);
                entry.targetScore = remapPidRecord(
                    entry.sourceScore,
                    entry.sourcePids,
                    entry.targetPids,
                );
                entry.targetBalloon = remapPidRecord(
                    entry.sourceBalloon,
                    entry.sourcePids,
                    entry.targetPids,
                );
                saveProblemDraftIdempotencyState();

                // Re-read and re-hash every source after cloning. A concurrent
                // source edit leaves hidden draft documents behind but can
                // never switch the contest to a mixed snapshot.
                const finalPreflight = await buildProblemDraftPreflight(request);
                if (!finalPreflight.safe_to_apply
                    || finalPreflight.preflight_id !== request.preflightId
                    || !sameNumberArray(finalPreflight._tdoc.pids, entry.sourcePids)) {
                    throw new OrchestratorProblemDraftConflictError();
                }
                const remainingRecords = await RecordModel.coll.countDocuments({
                    domainId: DOMAIN,
                    contest: finalPreflight._tdoc.docId,
                }, { limit: 1 });
                if (remainingRecords > 0) throw new OrchestratorProblemDraftBlockedError();

                const marker = {
                    version: 1,
                    operationId: request.operationId,
                    preflightId: request.preflightId,
                    approvalId: request.approvalId,
                    sourcePids: entry.sourcePids,
                    targetPids: entry.targetPids,
                    scoreMigrated: entry.hasScore,
                    balloonMigrated: entry.hasBalloon,
                    appliedAt: new Date(),
                };
                const contestFilter = {
                    domainId: DOMAIN,
                    docType: DocumentModel.TYPE_CONTEST,
                    docId: finalPreflight._tdoc.docId,
                    rule: 'oi',
                    beginAt: { $gt: new Date() },
                    pids: entry.sourcePids,
                    score: entry.hasScore ? entry.sourceScore : { $exists: false },
                    balloon: entry.hasBalloon ? entry.sourceBalloon : { $exists: false },
                };
                const contestUpdate = {
                    pids: entry.targetPids,
                    orchestratorFileIo: marker,
                    ...(entry.hasScore ? { score: entry.targetScore } : {}),
                    ...(entry.hasBalloon ? { balloon: entry.targetBalloon } : {}),
                };
                let updated = await DocumentModel.coll.findOneAndUpdate(
                    contestFilter,
                    {
                        $set: contestUpdate,
                    },
                    { returnDocument: 'after' },
                );
                if (!updated) {
                    updated = await ContestModel.get(
                        DOMAIN,
                        finalPreflight._tdoc.docId,
                    );
                    if (!contestMatchesAppliedProblemDraft(updated, entry)) {
                        throw new OrchestratorProblemDraftConflictError();
                    }
                }
                entry.complete = true;
                entry.completedAt = new Date().toISOString();
                saveProblemDraftIdempotencyState();
                return problemDraftResult(entry);
            } finally {
                contestDraftMutations.delete(request.tid);
            }
        })();

        if (!active) {
            problemDraftInFlight.set(request.operationId, { fingerprint, promise: work });
        }
        try {
            this.response.body = await work;
        } finally {
            if (!active) problemDraftInFlight.delete(request.operationId);
        }
    }
}

class OrchestratorMaterialHandler extends Handler {
    noCheckPermView = true;

    async post() {
        const headerToken = this.request.headers['x-orchestrator-token'];
        if (!tokenMatches(headerToken)) throw new InvalidTokenError('orchestrator');

        const request = parseMaterialPublicationRequest(this.request.body || {});
        const fingerprint = materialPublicationFingerprint(request);
        if (request.publicationId !== fingerprint) {
            throw new BadRequestError('orchestrator material publication id');
        }
        let entry = materialIdempotencyState.entries[request.publicationId];
        if (entry && entry.fingerprint !== fingerprint) {
            throw new OrchestratorMaterialConflictError();
        }
        const active = materialInFlight.get(request.publicationId);
        if (active && active.fingerprint !== fingerprint) {
            throw new OrchestratorMaterialConflictError();
        }

        const work = active?.promise || (async () => {
            if (!entry) {
                entry = {
                    publicationId: request.publicationId,
                    tid: request.tid,
                    revision: request.revision,
                    fingerprint,
                    attachments: request.attachments.map(({ name, sha256, size }) => ({
                        name,
                        sha256,
                        size,
                    })),
                    uploaded: [],
                    complete: false,
                    createdAt: new Date().toISOString(),
                };
                materialIdempotencyState.entries[request.publicationId] = entry;
                saveMaterialIdempotencyState();
            }

            acquireContestMaterialMutation(request.tid);
            try {
                const tid = new ObjectId(request.tid);
                let tdoc = await ContestModel.get(DOMAIN, tid);
                const beginAt = dateMs(tdoc.beginAt);
                if (beginAt === null || Date.now() >= beginAt) {
                    throw new OrchestratorMaterialBlockedError();
                }
                if (tdoc.rule !== 'oi') {
                    throw new BadRequestError('orchestrator material contest rule');
                }
                const previousMarker = tdoc.orchestratorMaterials
                    ? JSON.parse(JSON.stringify(tdoc.orchestratorMaterials))
                    : null;
                if (previousMarker) {
                    if (await verifyPublishedMaterials(tdoc, entry)) {
                        entry.complete = true;
                        entry.completedAt ||= new Date().toISOString();
                        saveMaterialIdempotencyState();
                        return materialResult(entry);
                    }
                    if (!isOwnedMaterialMarker(previousMarker)) {
                        throw new OrchestratorMaterialConflictError();
                    }
                }

                const currentPrivate = Array.isArray(tdoc.privateFiles)
                    ? tdoc.privateFiles
                    : [];
                const reservedPrivate = currentPrivate.filter(
                    (item) => MATERIAL_ATTACHMENT_NAMES.has(item?.name),
                );
                if ((!previousMarker && reservedPrivate.length)
                    || (previousMarker
                        && (reservedPrivate.length !== MATERIAL_ATTACHMENT_NAMES.size
                            || [...MATERIAL_ATTACHMENT_NAMES].some(
                                (name) => reservedPrivate.filter(
                                    (item) => item?.name === name,
                                ).length !== 1,
                            )))) {
                    throw new OrchestratorMaterialConflictError();
                }

                const publishedFiles = [];
                for (const attachment of request.attachments) {
                    const target = materialStoragePath(request.tid, attachment.name);
                    await StorageModel.put(target, attachment.content, 1);
                    const meta = await StorageModel.getMeta(target);
                    if (!meta || Number(meta.size) !== attachment.size) {
                        throw new OrchestratorMaterialConflictError();
                    }
                    const source = await StorageModel.get(target);
                    const verified = await exactStreamSha256(source, MATERIAL_MAX_BYTES);
                    if (verified.size !== attachment.size
                        || verified.sha256 !== attachment.sha256) {
                        throw new OrchestratorMaterialConflictError();
                    }
                    publishedFiles.push({
                        _id: attachment.name,
                        name: attachment.name,
                        size: Number(meta.size),
                        lastModified: meta.lastModified,
                        etag: String(meta.etag || ''),
                    });
                    if (!entry.uploaded.includes(attachment.name)) {
                        entry.uploaded.push(attachment.name);
                        entry.uploaded.sort();
                        saveMaterialIdempotencyState();
                    }
                }

                tdoc = await ContestModel.get(DOMAIN, tid);
                const commitBeginAt = dateMs(tdoc.beginAt);
                if (commitBeginAt === null || Date.now() >= commitBeginAt) {
                    throw new OrchestratorMaterialBlockedError();
                }
                const commitMarker = tdoc.orchestratorMaterials || null;
                const commitReserved = (tdoc.privateFiles || []).filter(
                    (item) => MATERIAL_ATTACHMENT_NAMES.has(item?.name),
                );
                if (tdoc.rule !== 'oi'
                    || !sameJsonValue(commitMarker, previousMarker)
                    || (!previousMarker && commitReserved.length)
                    || (previousMarker
                        && (commitReserved.length !== MATERIAL_ATTACHMENT_NAMES.size
                            || [...MATERIAL_ATTACHMENT_NAMES].some(
                                (name) => commitReserved.filter(
                                    (item) => item?.name === name,
                                ).length !== 1,
                            )))) {
                    throw new OrchestratorMaterialConflictError();
                }
                const remaining = (tdoc.privateFiles || []).filter(
                    (item) => !MATERIAL_ATTACHMENT_NAMES.has(item?.name),
                );
                await ContestModel.edit(DOMAIN, tid, {
                    privateFiles: remaining.concat(publishedFiles),
                    orchestratorMaterials: materialMarker(request, fingerprint),
                });

                const applied = await ContestModel.get(DOMAIN, tid);
                if (!await verifyPublishedMaterials(applied, entry)) {
                    throw new OrchestratorMaterialConflictError();
                }
                entry.complete = true;
                entry.completedAt = new Date().toISOString();
                saveMaterialIdempotencyState();
                return materialResult(entry);
            } finally {
                contestMaterialMutations.delete(request.tid);
            }
        })();

        if (!active) materialInFlight.set(
            request.publicationId,
            { fingerprint, promise: work },
        );
        try {
            this.response.body = await work;
        } finally {
            if (!active) materialInFlight.delete(request.publicationId);
        }
    }
}

class OrchestratorNotifyHandler extends Handler {
    noCheckPermView = true;

    async post() {
        const headerToken = this.request.headers['x-orchestrator-token'];
        if (!tokenMatches(headerToken)) throw new InvalidTokenError('orchestrator');

        const body = this.request.body || {};
        if (!body || Array.isArray(body) || typeof body !== 'object'
            || Object.keys(body).some((key) => !NOTIFICATION_FIELDS.has(key))) {
            throw new BadRequestError('orchestrator notification payload');
        }
        const notificationId = String(body.notification_id || '');
        const purpose = String(body.purpose || '');
        const uid = Number(body.uid);
        if (!/^[0-9a-f]{64}$/.test(notificationId)
            || purpose !== 'seat_ready'
            || !Number.isSafeInteger(uid)
            || uid <= 1) {
            throw new BadRequestError('orchestrator notification payload');
        }
        if (!NOTIFICATION_ALLOWED_HOSTS.size) {
            throw new BadRequestError('orchestrator notification allowed hosts');
        }

        const payload = {
            purpose,
            uid,
            contestTitle: notificationText(body, 'contest_title', 512),
            desktopUrl: normalizeNotificationUrl(
                notificationText(body, 'desktop_url', 2048),
            ),
            candidate: notificationText(body, 'candidate', 128),
            studentPassword: notificationText(
                body,
                'student_password',
                256,
                { trim: false },
            ),
            availableAt: notificationText(
                body,
                'available_at',
                128,
                { optional: true },
            ),
        };
        const fingerprint = notificationFingerprint(payload);
        let current = notificationIdempotencyState.entries[notificationId];
        if (current && current.fingerprint !== fingerprint) {
            throw new OrchestratorNotificationConflictError();
        }
        const active = notificationInFlight.get(notificationId);
        if (active && active.fingerprint !== fingerprint) {
            throw new OrchestratorNotificationConflictError();
        }

        const content = notificationContent(payload, notificationId);
        const work = active?.promise || (async () => {
            current = notificationIdempotencyState.entries[notificationId];
            if (!current) {
                current = {
                    fingerprint,
                    messageId: '',
                    complete: false,
                    createdAt: new Date().toISOString(),
                };
                notificationIdempotencyState.entries[notificationId] = current;
                // Persist the reservation before creating the native message.
                // A changed payload can never reuse this notification id.
                saveNotificationIdempotencyState();
            }
            if (current.complete) return current.messageId;

            // MessageModel.send is the native Hydro path: it inserts the
            // message, broadcasts it, and increments unreadMsg. The exact
            // content lookup closes the usual response-loss/restart retry gap.
            let mdoc = await MessageModel.coll.findOne({ from: 1, to: uid, content });
            if (!mdoc) {
                mdoc = await MessageModel.send(
                    1,
                    uid,
                    content,
                    MessageModel.FLAG_RICHTEXT | MessageModel.FLAG_UNREAD,
                );
            }
            current.messageId = mdoc?._id ? String(mdoc._id) : '';
            current.complete = true;
            saveNotificationIdempotencyState();
            return current.messageId;
        })();

        if (!active) notificationInFlight.set(notificationId, { fingerprint, promise: work });
        try {
            this.response.body = {
                notification_id: notificationId,
                message_id: await work,
            };
        } finally {
            if (!active) notificationInFlight.delete(notificationId);
        }
    }
}

class OrchestratorSubmitHandler extends Handler {
    noCheckPermView = true;

    async post() {
        const headerToken = this.request.headers['x-orchestrator-token'];
        if (!tokenMatches(headerToken)) throw new InvalidTokenError('orchestrator');

        const body = this.request.body || {};
        const tidText = String(body.tid || '').toLowerCase();
        const uid = Number(body.uid);
        const pid = String(body.pid || '');
        const code = String(body.code || '');
        const submissionId = String(body.submission_id || '');
        const submissionKind = String(body.submission_kind || 'final');
        const acceptedAtMs = Number(body.accepted_at_ms || 0);
        if (!ObjectId.isValid(tidText)
            || !Number.isSafeInteger(uid)
            || uid <= 0
            || !pid
            || !code
            || !['final', 'realtime'].includes(submissionKind)
            || (submissionKind === 'realtime'
                && acceptedAtMs !== 0
                && (!Number.isSafeInteger(acceptedAtMs) || acceptedAtMs <= 0))
            || !/^[0-9a-f]{64}$/.test(submissionId)) {
            throw new BadRequestError('orchestrator payload');
        }
        if (Buffer.byteLength(code, 'utf8') > MAX_CODE_BYTES) {
            throw new OrchestratorCodeTooLargeError();
        }

        const lang = String(body.lang || pickCc14());
        const normalized = code.replace(/\r\n/g, '\n');
        const fingerprint = payloadFingerprint(tidText, uid, pid, lang, normalized);
        const persisted = idempotencyState.entries[submissionId];
        if (persisted && persisted.fingerprint !== fingerprint) {
            throw new OrchestratorSubmissionConflictError();
        }
        const pending = inFlight.get(submissionId);
        if (pending && pending.fingerprint !== fingerprint) {
            throw new OrchestratorSubmissionConflictError();
        }
        if (pending) {
            this.response.body = { rid: await pending.promise };
            return;
        }
        if (persisted?.complete) {
            this.response.body = { rid: persisted.rid };
            return;
        }
        if (persisted) {
            // The reservation is durable before RecordModel.add.  If the
            // process died (or RecordModel.add threw) before the returned RID
            // was durably attached, the insert may or may not exist.  Never
            // guess and never call add again: an operator can disambiguate by
            // the bound payload and reservation timestamp without risking a
            // duplicate OJ submission.
            if (!persisted.rid) {
                throw new OrchestratorSubmissionAmbiguousError();
            }
            // A durable but incomplete journal bypasses mutable eligibility
            // checks and resumes only its missing side effects. New entries
            // persist both docIds; old journals fall back to minimal reads.
            const recovery = (async () => {
                let problemDocId = persisted.problemDocId;
                if (problemDocId === undefined || problemDocId === null) {
                    const recoveryProblem = await ProblemModel.get(DOMAIN, pid);
                    if (!recoveryProblem) throw new ProblemNotFoundError(DOMAIN, pid);
                    problemDocId = recoveryProblem.docId;
                }
                let contestDocId = persisted.contestDocId;
                if (contestDocId === undefined || contestDocId === null) {
                    const recoveryContest = await ContestModel.get(DOMAIN, new ObjectId(tidText));
                    contestDocId = recoveryContest.docId;
                }
                persisted.problemDocId = problemDocId;
                persisted.contestDocId = String(contestDocId);
                persisted.uid = uid;
                saveIdempotencyState();
                return completeSubmissionEntry(
                    persisted,
                    submissionContestDocId(contestDocId),
                    problemDocId,
                    uid,
                );
            })();
            inFlight.set(submissionId, { fingerprint, promise: recovery });
            try {
                this.response.body = { rid: await recovery };
            } finally {
                inFlight.delete(submissionId);
            }
            return;
        }

        // New requests persist their reservation before RecordModel.add. The
        // private record markers below let the status endpoint resolve a lost
        // response by exact identity; normal submission retries never call add
        // again while that reservation is incomplete.

        acquireContestSubmissionLease(tidText);
        try {
        const tid = new ObjectId(tidText);
        const pdoc = await ProblemModel.get(DOMAIN, pid);
        if (!pdoc) throw new ProblemNotFoundError(DOMAIN, pid);
        const tdoc = await ContestModel.get(DOMAIN, tid);
        if (!tdoc.pids?.includes(pdoc.docId)) throw new ProblemNotFoundError(DOMAIN, pid);
        const tsdoc = await ContestModel.getStatus(DOMAIN, tid, uid);
        if (!tsdoc) throw new ContestNotAttendedError(tid);
        if (submissionKind === 'realtime' && tdoc.rule !== 'oi') {
            throw new BadRequestError('orchestrator contest rule');
        }
        const configuredLang = SettingModel.langs?.[lang];
        const allowed = typeof pdoc.config === 'object'
            && Array.isArray(pdoc.config?.langs)
            && pdoc.config.langs.length
            ? pdoc.config.langs.includes(lang)
            : true;
        if (!configuredLang || configuredLang.disabled || !allowed) {
            throw new BadRequestError('orchestrator language');
        }

        const current = idempotencyState.entries[submissionId];
        if (current && current.fingerprint !== fingerprint) {
            throw new OrchestratorSubmissionConflictError();
        }

        const active = inFlight.get(submissionId);
        if (active && active.fingerprint !== fingerprint) {
            throw new OrchestratorSubmissionConflictError();
        }
        // A retry may arrive after the effective personal deadline because the
        // original HTTP response was lost. Existing/in-flight idempotency
        // entries must still return their original RID; only a genuinely new
        // realtime action is time-gated.
        const acceptedInWindow = Number.isSafeInteger(acceptedAtMs)
            && acceptedAtMs > 0
            && acceptedAtInContestWindow(tdoc, tsdoc, acceptedAtMs);
        if (submissionKind === 'realtime'
            && !current
            && !active
            && (acceptedAtMs > 0
                ? !acceptedInWindow
                : !ContestModel.isOngoing(tdoc, tsdoc))) {
            throw new ContestNotLiveError(DOMAIN, tid);
        }

        const work = active?.promise || (async () => {
            let entry = idempotencyState.entries[submissionId];
            if (!entry) {
                entry = {
                    fingerprint,
                    rid: '',
                    contestDocId: String(tdoc.docId),
                    problemDocId: pdoc.docId,
                    publicPid: pid,
                    uid,
                    lang,
                    statusUpdated: false,
                    problemCounted: false,
                    userCounted: false,
                    complete: false,
                    phase: 'reserved',
                    createdAt: new Date().toISOString(),
                };
                idempotencyState.entries[submissionId] = entry;
                // This reservation must reach stable storage before the first
                // irreversible OJ write.  A failure here creates no record.
                saveIdempotencyState();
                const rid = await RecordModel.add(
                    DOMAIN,
                    pdoc.docId,
                    uid,
                    lang,
                    normalized,
                    true,
                    {
                        contest: tdoc.docId,
                        files: {
                            orchestratorSubmissionId: submissionId,
                            orchestratorPayloadSha256: fingerprint,
                        },
                        type: 'judge',
                    },
                );
                entry.rid = String(rid);
                entry.phase = 'record_created';
                saveIdempotencyState();
            }
            return completeSubmissionEntry(entry, tdoc.docId, pdoc.docId, uid);
        })();

        if (!active) inFlight.set(submissionId, { fingerprint, promise: work });
        try {
            this.response.body = { rid: await work };
        } finally {
            if (!active) inFlight.delete(submissionId);
        }
        } finally {
            releaseContestSubmissionLease(tidText);
        }
    }
}

exports.apply = (ctx) => {
    if (!TOKEN || TOKEN.length < 32) {
        throw new Error('orchestrator token is missing or shorter than 32 characters');
    }
    if (!Number.isSafeInteger(IDEMPOTENCY_MAX_ENTRIES) || IDEMPOTENCY_MAX_ENTRIES < 100) {
        throw new Error('ORCHESTRATOR_IDEMPOTENCY_MAX_ENTRIES must be an integer of at least 100');
    }
    if (!Number.isSafeInteger(NOTIFICATION_IDEMPOTENCY_MAX_ENTRIES)
        || NOTIFICATION_IDEMPOTENCY_MAX_ENTRIES < 100) {
        throw new Error(
            'ORCHESTRATOR_NOTIFICATION_IDEMPOTENCY_MAX_ENTRIES must be an integer of at least 100',
        );
    }
    if (!Number.isSafeInteger(PROBLEM_DRAFT_IDEMPOTENCY_MAX_ENTRIES)
        || PROBLEM_DRAFT_IDEMPOTENCY_MAX_ENTRIES < 100) {
        throw new Error(
            'ORCHESTRATOR_PROBLEM_DRAFT_IDEMPOTENCY_MAX_ENTRIES must be an integer of at least 100',
        );
    }
    if (!Number.isSafeInteger(MATERIAL_IDEMPOTENCY_MAX_ENTRIES)
        || MATERIAL_IDEMPOTENCY_MAX_ENTRIES < 100) {
        throw new Error(
            'ORCHESTRATOR_MATERIAL_IDEMPOTENCY_MAX_ENTRIES must be an integer of at least 100',
        );
    }
    if (!Number.isSafeInteger(MATERIAL_MAX_BYTES)
        || MATERIAL_MAX_BYTES < 1024 * 1024
        || MATERIAL_MAX_BYTES > 512 * 1024 * 1024) {
        throw new Error(
            'ORCHESTRATOR_MATERIAL_MAX_BYTES must be between 1 MiB and 512 MiB',
        );
    }
    if (!Number.isSafeInteger(PROBLEM_DRAFT_MAX_PROBLEMS)
        || PROBLEM_DRAFT_MAX_PROBLEMS < 1
        || PROBLEM_DRAFT_MAX_PROBLEMS > 100) {
        throw new Error('ORCHESTRATOR_PROBLEM_DRAFT_MAX_PROBLEMS must be between 1 and 100');
    }
    if (!Number.isSafeInteger(PROBLEM_DRAFT_MAX_FILES)
        || PROBLEM_DRAFT_MAX_FILES < 100
        || PROBLEM_DRAFT_MAX_FILES > 100000) {
        throw new Error('ORCHESTRATOR_PROBLEM_DRAFT_MAX_FILES must be between 100 and 100000');
    }
    if (!Number.isSafeInteger(PROBLEM_DRAFT_MAX_HASH_BYTES)
        || PROBLEM_DRAFT_MAX_HASH_BYTES < 1024 * 1024) {
        throw new Error('ORCHESTRATOR_PROBLEM_DRAFT_MAX_HASH_BYTES must be at least 1 MiB');
    }
    if (!Number.isSafeInteger(PROBLEM_DRAFT_MAX_LINE_BYTES)
        || PROBLEM_DRAFT_MAX_LINE_BYTES < 1024 * 1024
        || PROBLEM_DRAFT_MAX_LINE_BYTES > PROBLEM_DRAFT_MAX_HASH_BYTES) {
        throw new Error(
            'ORCHESTRATOR_PROBLEM_DRAFT_MAX_LINE_BYTES must be at least 1 MiB and no larger than the hash budget',
        );
    }
    if ([...NOTIFICATION_ALLOWED_HOSTS].some((host) => !isExactDnsHostname(host))) {
        throw new Error(
            'ORCHESTRATOR_NOTIFY_ALLOWED_HTTPS_HOSTS accepts exact DNS hostnames only',
        );
    }
    // Prove that persistence is writable before accepting a request. Otherwise
    // a successful RecordModel.add followed by a filesystem error could not be
    // recognized when the orchestrator retries.
    saveIdempotencyState();
    if (NOTIFICATION_ALLOWED_HOSTS.size) saveNotificationIdempotencyState();
    saveProblemDraftIdempotencyState();
    saveMaterialIdempotencyState();
    // Keep the notification route under /orchestrator/submit*: the deployment
    // Caddy rule already makes this whole prefix return 404 publicly, while
    // the orchestrator can reach Hydro's loopback listener directly.
    ctx.Route(
        'orchestrator_problem_fileio',
        '/orchestrator/submit/problem-fileio',
        OrchestratorProblemFileIoHandler,
    );
    ctx.Route(
        'orchestrator_materials',
        '/orchestrator/submit/materials',
        OrchestratorMaterialHandler,
    );
    ctx.Route(
        'orchestrator_notify',
        '/orchestrator/submit/notify',
        OrchestratorNotifyHandler,
    );
    ctx.Route(
        'orchestrator_submit_status',
        '/orchestrator/submit/status',
        OrchestratorSubmissionStatusHandler,
    );
    ctx.Route(
        'orchestrator_submit',
        '/orchestrator/submit',
        OrchestratorSubmitHandler,
    );
};
