const assert = require('node:assert/strict');
const crypto = require('node:crypto');
const fs = require('node:fs');
const Module = require('node:module');
const os = require('node:os');
const path = require('node:path');
const { Readable } = require('node:stream');
const { after, before, beforeEach, test } = require('node:test');

const tempDir = fs.mkdtempSync(path.join(os.tmpdir(), 'hydro-orchestrator-material-test-'));
process.env.ORCHESTRATOR_TOKEN = 'test-token-that-is-at-least-thirty-two-characters';
process.env.ORCHESTRATOR_IDEMPOTENCY_FILE = path.join(tempDir, 'submissions.json');
process.env.ORCHESTRATOR_NOTIFICATION_IDEMPOTENCY_FILE = path.join(tempDir, 'notifications.json');
process.env.ORCHESTRATOR_PROBLEM_DRAFT_IDEMPOTENCY_FILE = path.join(tempDir, 'problem-drafts.json');
process.env.ORCHESTRATOR_MATERIAL_IDEMPOTENCY_FILE = path.join(tempDir, 'materials.json');

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
class MockObjectId {
    constructor(value) { this.value = String(value); }
    toString() { return this.value; }
    static isValid(value) { return /^[0-9a-f]{24}$/i.test(String(value)); }
}

const storage = new Map();
let putCalls = 0;
let editCalls = 0;
let failSecondPut = false;
let contest;

const StorageModel = {
    async put(target, content) {
        putCalls += 1;
        if (failSecondPut && putCalls === 2) throw new Error('fixture storage failure');
        const value = Buffer.from(content);
        storage.set(target, {
            content: value,
            meta: {
                size: value.length,
                etag: crypto.createHash('sha256').update(value).digest('hex'),
                lastModified: new Date('2026-08-12T00:00:00Z'),
            },
        });
    },
    async get(target) {
        const item = storage.get(target);
        if (!item) throw new Error(`missing fixture storage: ${target}`);
        return Readable.from([item.content]);
    },
    async getMeta(target) {
        return storage.get(target)?.meta || null;
    },
};

const ContestModel = {
    async get() { return contest; },
    async edit(_domain, _tid, update) {
        editCalls += 1;
        contest = { ...contest, ...update };
        return contest;
    },
};

const hydroMock = {
    BadRequestError,
    ContestModel,
    ContestNotLiveError: class extends UserFacingError {},
    ContestNotAttendedError: class extends UserFacingError {},
    CreateError,
    DomainModel: {},
    Handler,
    InvalidTokenError,
    MessageModel: {},
    ObjectId: MockObjectId,
    ProblemModel: {},
    ProblemNotFoundError: class extends UserFacingError {},
    RecordModel: {},
    SettingModel: { langs: {} },
    StorageModel,
    UserFacingError,
};

const routes = {};
const originalLoad = Module._load;

function applyPlugin() {
    const plugin = require('../index.js');
    plugin.apply({
        Route(name, route, handler) {
            routes[name] = { route, handler };
        },
    });
}

before(() => {
    Module._load = function load(request, parent, isMain) {
        if (request === 'hydrooj') return hydroMock;
        return originalLoad.call(this, request, parent, isMain);
    };
    applyPlugin();
});

beforeEach(() => {
    storage.clear();
    putCalls = 0;
    editCalls = 0;
    failSecondPut = false;
    contest = {
        docId: new MockObjectId('1234567890abcdef12345678'),
        beginAt: new Date(Date.now() + 60_000),
        endAt: new Date(Date.now() + 3_600_000),
        rule: 'oi',
        privateFiles: [],
    };
});

after(() => {
    Module._load = originalLoad;
    fs.rmSync(tempDir, { recursive: true, force: true });
});

function sha256(value) {
    return crypto.createHash('sha256').update(value).digest('hex');
}

function materialBody(label = 'default') {
    const paper = Buffer.from('%PDF-1.7\nfixture paper');
    const testdata = Buffer.from('fixture tar gzip bytes');
    const tid = '1234567890abcdef12345678';
    const revision = `release-${label}`;
    const attachments = [
            {
                name: '01_比赛题面.pdf',
                sha256: sha256(paper),
                content_base64: paper.toString('base64'),
            },
            {
                name: '02_辅助自测数据.tar.gz',
                sha256: sha256(testdata),
                content_base64: testdata.toString('base64'),
            },
        ];
    const publicationId = sha256(JSON.stringify([
        'noi-material-publication-v1',
        tid,
        revision,
        attachments.map(({ name, sha256: digest, content_base64: encoded }) => ({
            name,
            sha256: digest,
            size: Buffer.from(encoded, 'base64').length,
        })),
    ]));
    return {
        publication_id: publicationId,
        tid,
        revision,
        attachments,
    };
}

async function publish(body = materialBody(), headers = {}) {
    const handler = new routes.orchestrator_materials.handler();
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

test('registers material publication below the private submit prefix', () => {
    assert.equal(
        routes.orchestrator_materials.route,
        '/orchestrator/submit/materials',
    );
});

test('publishes the exact two immutable private contest attachments', async () => {
    const body = materialBody('exact');
    const result = await publish(body);

    assert.equal(result.status, 'published');
    assert.equal(result.publication_id, body.publication_id);
    assert.deepEqual(
        result.attachments.map((item) => item.name),
        ['01_比赛题面.pdf', '02_辅助自测数据.tar.gz'],
    );
    assert.equal(putCalls, 2);
    assert.equal(editCalls, 1);
    assert.equal(contest.privateFiles.length, 2);
    assert.equal(contest.orchestratorMaterials.publicationId, body.publication_id);
    for (const attachment of body.attachments) {
        const target = `contest/system/${body.tid}/private/${attachment.name}`;
        assert.equal(sha256(storage.get(target).content), attachment.sha256);
    }
});

test('returns the original receipt for an identical retry without rewriting files', async () => {
    const body = materialBody('retry');
    const first = await publish(body);
    const writes = putCalls;
    const edits = editCalls;
    const second = await publish(body);

    assert.deepEqual(second, first);
    assert.equal(putCalls, writes);
    assert.equal(editCalls, edits);
});

test('rejects changed bytes under an existing derived publication id', async () => {
    const body = materialBody('conflict');
    await publish(body);
    const changed = structuredClone(body);
    const replacement = Buffer.from('%PDF-1.7\nchanged');
    changed.attachments[0].sha256 = sha256(replacement);
    changed.attachments[0].content_base64 = replacement.toString('base64');

    await assert.rejects(publish(changed), (error) => error.code === 400);
    assert.equal(putCalls, 2);
    assert.equal(editCalls, 1);
});

test('replaces an earlier orchestrator-owned release before the contest starts', async () => {
    const first = materialBody('first-release');
    const second = materialBody('second-release');
    const changedPaper = Buffer.from('%PDF-1.7\nsecond approved paper');
    second.attachments[0].sha256 = sha256(changedPaper);
    second.attachments[0].content_base64 = changedPaper.toString('base64');
    second.publication_id = sha256(JSON.stringify([
        'noi-material-publication-v1',
        second.tid,
        second.revision,
        second.attachments.map(({ name, sha256: digest, content_base64: encoded }) => ({
            name,
            sha256: digest,
            size: Buffer.from(encoded, 'base64').length,
        })),
    ]));

    await publish(first);
    const result = await publish(second);

    assert.equal(result.publication_id, second.publication_id);
    assert.equal(putCalls, 4);
    assert.equal(editCalls, 2);
    assert.equal(contest.orchestratorMaterials.publicationId, second.publication_id);
});

test('never replaces a reserved file unless the prior marker is valid', async () => {
    contest.orchestratorMaterials = { version: 1, publicationId: 'invalid' };
    contest.privateFiles = [
        { name: '01_比赛题面.pdf', size: 12 },
        { name: '02_辅助自测数据.tar.gz', size: 12 },
    ];
    await assert.rejects(
        publish(materialBody('invalid-prior-marker')),
        (error) => error.code === 409,
    );
    assert.equal(putCalls, 0);
    assert.equal(editCalls, 0);
});

test('fails closed after the contest starts and before any storage write', async () => {
    contest.beginAt = new Date(Date.now() - 1);
    await assert.rejects(
        publish(materialBody('started')),
        (error) => error.code === 422,
    );
    assert.equal(putCalls, 0);
    assert.equal(editCalls, 0);
});

test('does not overwrite a teacher file using a reserved product filename', async () => {
    contest.privateFiles = [{ name: '01_比赛题面.pdf', size: 12 }];
    await assert.rejects(
        publish(materialBody('reserved')),
        (error) => error.code === 409,
    );
    assert.equal(putCalls, 0);
    assert.equal(editCalls, 0);
});

test('retries a partial storage upload and publishes only after both hashes verify', async () => {
    failSecondPut = true;
    const body = materialBody('partial');
    await assert.rejects(publish(body), /fixture storage failure/);
    assert.equal(editCalls, 0);
    failSecondPut = false;

    const result = await publish(body);
    assert.equal(result.status, 'published');
    assert.equal(editCalls, 1);
    assert.equal(contest.privateFiles.length, 2);
});

test('recovers a committed contest marker after the local receipt was interrupted', async () => {
    const body = materialBody('recovery');
    await publish(body);
    const writes = putCalls;
    const edits = editCalls;
    const state = JSON.parse(fs.readFileSync(
        process.env.ORCHESTRATOR_MATERIAL_IDEMPOTENCY_FILE,
        'utf8',
    ));
    state.entries[body.publication_id].complete = false;
    delete state.entries[body.publication_id].completedAt;
    fs.writeFileSync(
        process.env.ORCHESTRATOR_MATERIAL_IDEMPOTENCY_FILE,
        `${JSON.stringify(state)}\n`,
    );
    delete require.cache[require.resolve('../index.js')];
    applyPlugin();

    const result = await publish(body);
    assert.equal(result.status, 'published');
    assert.equal(putCalls, writes);
    assert.equal(editCalls, edits);
});

test('rejects malformed base64 or a mismatched digest', async () => {
    const malformed = materialBody('malformed');
    malformed.attachments[0].content_base64 = '%%%%';
    await assert.rejects(publish(malformed), (error) => error.code === 400);

    const mismatch = materialBody('mismatch');
    mismatch.attachments[0].sha256 = '0'.repeat(64);
    await assert.rejects(publish(mismatch), (error) => error.code === 400);
    assert.equal(putCalls, 0);
});
