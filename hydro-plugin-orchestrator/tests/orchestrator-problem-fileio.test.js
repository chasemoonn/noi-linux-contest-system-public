const assert = require('node:assert/strict');
const crypto = require('node:crypto');
const fs = require('node:fs');
const Module = require('node:module');
const os = require('node:os');
const path = require('node:path');
const { Readable } = require('node:stream');
const { after, before, test } = require('node:test');

const tempDir = fs.mkdtempSync(path.join(os.tmpdir(), 'hydro-fileio-test-'));
process.env.ORCHESTRATOR_TOKEN = 'test-token-that-is-at-least-thirty-two-characters';
process.env.ORCHESTRATOR_IDEMPOTENCY_FILE = path.join(tempDir, 'submissions.json');
process.env.ORCHESTRATOR_NOTIFICATION_IDEMPOTENCY_FILE = path.join(tempDir, 'notifications.json');
process.env.ORCHESTRATOR_PROBLEM_DRAFT_IDEMPOTENCY_FILE = path.join(tempDir, 'problem-drafts.json');
process.env.ORCHESTRATOR_MATERIAL_IDEMPOTENCY_FILE = path.join(tempDir, 'materials.json');

class MockObjectId {
    constructor(value) {
        this.value = String(value);
    }

    toString() {
        return this.value;
    }

    static isValid(value) {
        return /^[0-9a-f]{24}$/i.test(String(value));
    }
}

class UserFacingError extends Error {
    constructor(...params) {
        super();
        this.params = params;
        this.code = 400;
    }
}

function CreateError(name, Base, _message, code) {
    return class extends Base {
        constructor(...params) {
            super(...params);
            this.name = name;
            this.code = code;
        }
    };
}

class BadRequestError extends UserFacingError {}
class InvalidTokenError extends UserFacingError {
    constructor(...params) {
        super(...params);
        this.code = 403;
    }
}
class Handler {}
class ProblemNotFoundError extends UserFacingError {}

const tidText = '1234567890abcdef12345678';
const originalPids = [101, 102];
const storage = new Map();
const aliases = new Map();
const documents = new Map();
let nextDocId = 500;
let contestRecords = 0;
let staleCloneRawReads = 0;
let cloneVerificationRawReads = 0;

function meta(name, content) {
    return {
        name,
        size: content.length,
        etag: crypto.createHash('sha256').update(content).digest('hex'),
    };
}

function makeProblem(docId, pid, title, content, input) {
    const config = Buffer.from(JSON.stringify({
        time: 1000,
        memory: 256,
        subtasks: [{ cases: [{ input: '1.in', output: '1.out' }] }],
    }));
    const output = Buffer.from('expected-output\n');
    const statementFile = Buffer.from(`attachment-${pid}`);
    const pdoc = {
        docId,
        pid,
        owner: 9,
        title,
        content,
        html: false,
        hidden: false,
        tag: ['source'],
        difficulty: 3,
        config: config.toString(),
        data: [meta('config.yaml', config), meta('1.in', input), meta('1.out', output)],
        additional_file: [meta('diagram.txt', statementFile)],
    };
    documents.set(docId, pdoc);
    aliases.set(pid, docId);
    storage.set(`problem/system/${docId}/testdata/config.yaml`, config);
    storage.set(`problem/system/${docId}/testdata/1.in`, input);
    storage.set(`problem/system/${docId}/testdata/1.out`, output);
    storage.set(`problem/system/${docId}/additional_file/diagram.txt`, statementFile);
}

makeProblem(
    101,
    'P1',
    'First problem',
    'Statement one',
    Buffer.from(' \r\nalpha  \r\n  beta\t \r\ngamma   \n\n'),
);
makeProblem(102, 'P2', 'Second problem', 'Statement two', Buffer.from('hidden-secret\r\n'));

const contest = {
    docId: new MockObjectId(tidText),
    title: 'CSP-J private clone test',
    owner: 9,
    rule: 'oi',
    beginAt: new Date(Date.now() + 60 * 60 * 1000),
    endAt: new Date(Date.now() + 3 * 60 * 60 * 1000),
    pids: [...originalPids],
    score: { 101: 40, 102: 60 },
    balloon: { 101: 'red', 102: { color: 'blue', name: 'B' } },
};

function findProblem(pid) {
    let key = pid;
    if (pid instanceof MockObjectId) key = pid.toString();
    if (typeof key === 'string' && /^\d+$/.test(key)) key = Number(key);
    if (typeof key === 'string' && aliases.has(key)) key = aliases.get(key);
    return documents.get(key) || null;
}

function parsedConfig(raw) {
    const config = JSON.parse(raw || '{}');
    return {
        type: config.type || 'default',
        subType: config.filename,
        count: 1,
        timeMin: Number(config.time || 1000),
        timeMax: Number(config.time || 1000),
        memoryMin: Number(config.memory || 256),
        memoryMax: Number(config.memory || 256),
        hackable: false,
        langs: ['cc'],
    };
}

async function toBuffer(value) {
    if (Buffer.isBuffer(value)) return value;
    const chunks = [];
    for await (const chunk of value) chunks.push(Buffer.from(chunk));
    return Buffer.concat(chunks);
}

const ProblemModel = {
    PROJECTION_PUBLIC: [
        'docId', 'pid', 'owner', 'title', 'content', 'html', 'hidden', 'tag',
        'difficulty', 'config', 'data', 'additional_file', 'reference',
    ],
    get: async (_domain, pid, _projection, rawConfig = false) => {
        const problem = findProblem(pid);
        if (!problem) return null;
        const result = {
            ...problem,
            tag: [...(problem.tag || [])],
            data: (problem.data || []).map((item) => ({ ...item })),
            additional_file: (problem.additional_file || []).map((item) => ({ ...item })),
            config: rawConfig ? problem.config : parsedConfig(problem.config),
            ...(problem.orchestratorFileIoClone
                ? { orchestratorFileIoClone: { ...problem.orchestratorFileIoClone } }
                : {}),
        };
        if (rawConfig && problem.orchestratorFileIoClone) {
            cloneVerificationRawReads++;
            if (staleCloneRawReads > 0) {
                if (Number.isFinite(staleCloneRawReads)) staleCloneRawReads--;
                result.hidden = false;
            }
        }
        return result;
    },
    add: async (_domain, pid, title, content, owner, tag, options) => {
        if (aliases.has(pid)) throw new Error('duplicate pid');
        const docId = nextDocId++;
        documents.set(docId, {
            docId,
            pid,
            title,
            content,
            owner,
            tag: [...tag],
            hidden: Boolean(options.hidden),
            difficulty: options.difficulty,
            html: false,
            config: '',
            data: [],
            additional_file: [],
        });
        aliases.set(pid, docId);
        return docId;
    },
    addTestdata: async (_domain, docId, name, value) => {
        const content = await toBuffer(value);
        storage.set(`problem/system/${docId}/testdata/${name}`, content);
        const problem = documents.get(docId);
        problem.data = problem.data.filter((item) => item.name !== name);
        problem.data.push(meta(name, content));
        if (['config.yaml', 'config.yml', 'Config.yaml', 'Config.yml'].includes(name)) {
            problem.config = content.toString();
        }
    },
    addAdditionalFile: async (_domain, docId, name, value) => {
        const content = await toBuffer(value);
        storage.set(`problem/system/${docId}/additional_file/${name}`, content);
        const problem = documents.get(docId);
        problem.additional_file = problem.additional_file.filter((item) => item.name !== name);
        problem.additional_file.push(meta(name, content));
    },
    edit: async (_domain, docId, values) => {
        Object.assign(documents.get(docId), values);
        return documents.get(docId);
    },
};

const StorageModel = {
    get: async (target) => {
        if (!storage.has(target)) throw new Error(`missing storage ${target}`);
        const content = Buffer.from(storage.get(target));
        if (target.endsWith('/101/testdata/1.in')) {
            const boundary = content.indexOf(0x0d) + 1;
            return Readable.from([
                content.subarray(0, boundary),
                content.subarray(boundary, boundary + 1),
                content.subarray(boundary + 1),
            ]);
        }
        return Readable.from([content]);
    },
};

const RecordModel = {
    coll: {
        countDocuments: async (query) => (query.contest ? contestRecords : 0),
    },
};

const DocumentModel = {
    TYPE_CONTEST: 30,
    TYPE_CONTEST_CLARIFICATION: 31,
    collStatus: {
        countDocuments: async () => 0,
    },
    coll: {
        countDocuments: async () => 0,
        findOneAndUpdate: async (query, update) => {
            const fieldMatches = (field) => {
                if (query[field]?.$exists === false) return contest[field] === undefined;
                return JSON.stringify(query[field]) === JSON.stringify(contest[field]);
            };
            const matches = query.domainId === 'system'
                && query.docType === 30
                && String(query.docId) === tidText
                && query.rule === contest.rule
                && query.beginAt.$gt < contest.beginAt
                && JSON.stringify(query.pids) === JSON.stringify(contest.pids)
                && fieldMatches('score')
                && fieldMatches('balloon');
            if (!matches) return null;
            Object.assign(contest, update.$set);
            return contest;
        },
    },
};

const hydroMock = {
    BadRequestError,
    ContestModel: {
        get: async () => contest,
    },
    ContestNotAttendedError: UserFacingError,
    ContestNotLiveError: UserFacingError,
    CreateError,
    DocumentModel,
    DomainModel: {},
    Handler,
    InvalidTokenError,
    MessageModel: {},
    ObjectId: MockObjectId,
    ProblemModel,
    ProblemNotFoundError,
    RecordModel,
    SettingModel: { langs: {} },
    StorageModel,
    UserFacingError,
};

const yamlMock = {
    load: (value) => JSON.parse(value),
    dump: (value) => JSON.stringify(value),
};

const routes = {};
const originalLoad = Module._load;

before(() => {
    Module._load = function load(request, parent, isMain) {
        if (request === 'hydrooj') return hydroMock;
        if (request === 'js-yaml') return yamlMock;
        return originalLoad.call(this, request, parent, isMain);
    };
    const plugin = require('../index.js');
    plugin.apply({
        Route(name, route, handler) {
            routes[name] = { route, handler };
        },
    });
});

after(() => {
    Module._load = originalLoad;
    fs.rmSync(tempDir, { recursive: true, force: true });
});

function id(value) {
    return crypto.createHash('sha256').update(value).digest('hex');
}

const requestedProblems = [
    { pid: 'P1', slug: 'books' },
    { pid: 'P2', slug: 'study' },
];

async function call(body, headers = {}) {
    const handler = new routes.orchestrator_problem_fileio.handler();
    handler.request = {
        headers: {
            'x-orchestrator-token': process.env.ORCHESTRATOR_TOKEN,
            ...headers,
        },
        body,
    };
    handler.response = {};
    await handler.post();
    return handler.response.body;
}

async function preflight() {
    return await call({
        action: 'preflight',
        tid: tidText,
        problems: requestedProblems,
    });
}

function resetContest() {
    delete contest.orchestratorFileIo;
    contest.pids = [...originalPids];
    contest.score = { 101: 40, 102: 60 };
    contest.balloon = { 101: 'red', 102: { color: 'blue', name: 'B' } };
    contest.beginAt = new Date(Date.now() + 60 * 60 * 1000);
}

test('startup materializes an empty private problem-draft journal', () => {
    const statePath = process.env.ORCHESTRATOR_PROBLEM_DRAFT_IDEMPOTENCY_FILE;
    const state = JSON.parse(fs.readFileSync(statePath, 'utf8'));
    assert.equal(state.version, 1);
    assert.deepEqual(state.entries, {});
    if (process.platform !== 'win32') {
        assert.equal(fs.statSync(statePath).mode & 0o777, 0o600);
    }
});

test('registers the file-I/O route below the private submit prefix', () => {
    assert.equal(
        routes.orchestrator_problem_fileio.route,
        '/orchestrator/submit/problem-fileio',
    );
});

test('preflight returns statements, safe config summaries, limits, and only input hashes', async () => {
    const result = await preflight();
    assert.equal(result.safe_to_apply, true);
    assert.match(result.preflight_id, /^[0-9a-f]{64}$/);
    assert.equal(result.problems[0].title, 'First problem');
    assert.equal(result.problems[0].content, 'Statement one');
    assert.equal(result.problems[0].config.type, 'default');
    assert.deepEqual(result.problems[0].time_ms, { min: 1000, max: 1000 });
    assert.deepEqual(result.problems[0].memory_mb, { min: 256, max: 256 });
    assert.deepEqual(result.problems[0].formal_input_sha256, [
        // Cross-language fixed vector shared with Python
        // normalized_input_digest: b'alpha\n  beta\ngamma'.
        'f8ffc1d08f50fa840a132bce6f26802122df69416096b9a6d0444875e97cfc15',
    ]);
    const serialized = JSON.stringify(result);
    assert.equal(serialized.includes('hidden-secret'), false);
    assert.equal(serialized.includes('1.in'), false);
    assert.equal(serialized.includes('1.out'), false);
});

test('preflight accepts the shared 64-character file slug contract', async () => {
    const slug = `a${'b'.repeat(63)}`;
    const result = await call({
        action: 'preflight',
        tid: tidText,
        problems: [
            { pid: 'P1', slug },
            { pid: 'P2', slug: 'study' },
        ],
    });
    assert.equal(result.safe_to_apply, true);
    assert.equal(result.problems[0].slug, slug);
});

test('approved apply creates hidden independent clones, swaps only contest pids, and retries once', async () => {
    const before = JSON.parse(JSON.stringify(originalPids.map((pid) => documents.get(pid))));
    const checked = await preflight();
    const request = {
        action: 'apply',
        tid: tidText,
        problems: requestedProblems,
        operation_id: id('apply-success'),
        approval_id: id('teacher-approval-success'),
        preflight_id: checked.preflight_id,
    };
    const first = await call(request);
    const cloneCount = documents.size;
    const retry = await call(request);

    assert.deepEqual(retry, first);
    assert.equal(documents.size, cloneCount);
    assert.deepEqual(contest.pids, first.pids);
    assert.equal(contest.orchestratorFileIo.operationId, request.operation_id);
    assert.deepEqual(contest.score, {
        [String(first.pids[0])]: 40,
        [String(first.pids[1])]: 60,
    });
    assert.deepEqual(contest.balloon, {
        [String(first.pids[0])]: 'red',
        [String(first.pids[1])]: { color: 'blue', name: 'B' },
    });
    assert.deepEqual(
        JSON.parse(JSON.stringify(originalPids.map((pid) => documents.get(pid)))),
        before,
    );
    for (const item of first.mapping) {
        const clone = documents.get(item.clone_doc_id);
        assert.equal(clone.hidden, true);
        assert.match(clone.content, /本场文件读写要求/);
        assert.equal(JSON.parse(clone.config).filename, item.slug);
        assert.deepEqual(
            clone.data.map((file) => file.name).sort(),
            ['1.in', '1.out', 'config.yaml'],
        );
        assert.deepEqual(
            clone.additional_file.map((file) => file.name),
            ['diagram.txt'],
        );
        assert.notEqual(clone.reference, true);
    }
});

test('restart recovers a CAS-complete contest from an incomplete filesystem journal', async () => {
    const statePath = process.env.ORCHESTRATOR_PROBLEM_DRAFT_IDEMPOTENCY_FILE;
    const state = JSON.parse(fs.readFileSync(statePath, 'utf8'));
    const operationId = id('apply-success');
    const entry = state.entries[operationId];
    entry.complete = false;
    delete entry.completedAt;
    fs.writeFileSync(statePath, `${JSON.stringify(state)}\n`, 'utf8');

    delete require.cache[require.resolve('../index.js')];
    const restarted = require('../index.js');
    restarted.apply({
        Route(name, route, handler) {
            routes[name] = { route, handler };
        },
    });
    const result = await call({
        action: 'apply',
        tid: tidText,
        problems: requestedProblems,
        operation_id: operationId,
        approval_id: entry.approvalId,
        preflight_id: entry.preflightId,
    });

    assert.equal(result.status, 'applied');
    assert.deepEqual(result.pids, contest.pids);
    const recovered = JSON.parse(fs.readFileSync(statePath, 'utf8'));
    assert.equal(recovered.entries[operationId].complete, true);
});

test('reusing an operation id with changed approval is a permanent conflict', async () => {
    const successfulEntry = Object.values(
        JSON.parse(fs.readFileSync(process.env.ORCHESTRATOR_PROBLEM_DRAFT_IDEMPOTENCY_FILE, 'utf8')).entries,
    ).find((entry) => entry.complete);
    await assert.rejects(
        call({
            action: 'apply',
            tid: tidText,
            problems: requestedProblems,
            operation_id: successfulEntry.operationId,
            approval_id: id('different-approval'),
            preflight_id: successfulEntry.preflightId,
        }),
        (error) => error.code === 409,
    );
});

test('preflight and apply refuse a contest after its start time', async () => {
    resetContest();
    const checked = await preflight();
    contest.beginAt = new Date(Date.now() - 1000);
    try {
        const blocked = await preflight();
        assert.equal(blocked.safe_to_apply, false);
        assert.ok(blocked.blockers.includes('contest_already_started'));
        await assert.rejects(
            call({
                action: 'apply',
                tid: tidText,
                problems: requestedProblems,
                operation_id: id('apply-after-start'),
                approval_id: id('approval-after-start'),
                preflight_id: checked.preflight_id,
            }),
            (error) => error.code === 422,
        );
    } finally {
        contest.beginAt = new Date(Date.now() + 60 * 60 * 1000);
    }
});

test('apply refuses any existing contest submission', async () => {
    resetContest();
    const checked = await preflight();
    contestRecords = 1;
    try {
        const blocked = await preflight();
        assert.equal(blocked.safe_to_apply, false);
        assert.ok(blocked.blockers.includes('contest_has_submissions'));
        await assert.rejects(
            call({
                action: 'apply',
                tid: tidText,
                problems: requestedProblems,
                operation_id: id('apply-after-record'),
                approval_id: id('approval-after-record'),
                preflight_id: checked.preflight_id,
            }),
            (error) => error.code === 422,
        );
    } finally {
        contestRecords = 0;
    }
});

test('apply refuses a changed contest problem order instead of overwriting it', async () => {
    resetContest();
    const checked = await preflight();
    contest.pids = [...originalPids].reverse();
    try {
        await assert.rejects(
            call({
                action: 'apply',
                tid: tidText,
                problems: requestedProblems,
                operation_id: id('apply-conflicting-contest'),
                approval_id: id('approval-conflicting-contest'),
                preflight_id: checked.preflight_id,
            }),
            (error) => error.code === 409,
        );
        assert.deepEqual(contest.pids, [...originalPids].reverse());
    } finally {
        contest.pids = [...originalPids];
    }
});

test('clone verification retries a stale first read and applies only after every check passes', async () => {
    resetContest();
    const checked = await preflight();
    staleCloneRawReads = 1;
    cloneVerificationRawReads = 0;
    try {
        const result = await call({
            action: 'apply',
            tid: tidText,
            problems: requestedProblems,
            operation_id: id('apply-stale-read-recovers'),
            approval_id: id('approval-stale-read-recovers'),
            preflight_id: checked.preflight_id,
        });
        assert.equal(result.status, 'applied');
        assert.ok(cloneVerificationRawReads >= 3);
        assert.deepEqual(contest.pids, result.pids);
    } finally {
        staleCloneRawReads = 0;
        cloneVerificationRawReads = 0;
        resetContest();
    }
});

test('clone verification keeps all checks strict and logs labels only after bounded retries', async () => {
    resetContest();
    const checked = await preflight();
    const operationId = id('apply-stale-read-persists');
    const originalConsoleError = console.error;
    const logs = [];
    staleCloneRawReads = Number.POSITIVE_INFINITY;
    cloneVerificationRawReads = 0;
    console.error = (...values) => logs.push(values.join(' '));
    try {
        await assert.rejects(
            call({
                action: 'apply',
                tid: tidText,
                problems: requestedProblems,
                operation_id: operationId,
                approval_id: id('approval-stale-read-persists'),
                preflight_id: checked.preflight_id,
            }),
            (error) => error.code === 409,
        );
        assert.equal(cloneVerificationRawReads, 10);
        assert.deepEqual(contest.pids, originalPids);
        assert.equal(contest.orchestratorFileIo, undefined);
        assert.deepEqual(logs, [
            '[noi-fileio] clone verification failed mismatches=hidden',
        ]);
        assert.equal(logs[0].includes(operationId), false);
        assert.equal(logs[0].includes('Statement one'), false);
        assert.equal(logs[0].includes('1.in'), false);
    } finally {
        console.error = originalConsoleError;
        staleCloneRawReads = 0;
        cloneVerificationRawReads = 0;
        resetContest();
    }
});
