const assert = require('node:assert/strict');
const crypto = require('node:crypto');
const fs = require('node:fs');
const Module = require('node:module');
const os = require('node:os');
const path = require('node:path');
const { after, before, test } = require('node:test');

const tempDir = fs.mkdtempSync(path.join(os.tmpdir(), 'hydro-orchestrator-sso-test-'));
const token = 'test-token-that-is-at-least-thirty-two-characters';
process.env.ORCHESTRATOR_TOKEN = token;
process.env.ORCHESTRATOR_TEACHER_ADMIN_URL = 'https://exam.example.test/admin';
process.env.ORCHESTRATOR_IDEMPOTENCY_FILE = path.join(tempDir, 'submissions.json');
process.env.ORCHESTRATOR_NOTIFICATION_IDEMPOTENCY_FILE = path.join(tempDir, 'notifications.json');
process.env.ORCHESTRATOR_PROBLEM_DRAFT_IDEMPOTENCY_FILE = path.join(tempDir, 'problem-drafts.json');
process.env.ORCHESTRATOR_MATERIAL_IDEMPOTENCY_FILE = path.join(tempDir, 'materials.json');

class UserFacingError extends Error {}
class PermissionError extends UserFacingError {}
class Handler {}
class MockObjectId {
    constructor(value) { this.value = String(value); }
    toString() { return this.value; }
    static isValid(value) { return /^[0-9a-f]{24}$/i.test(String(value)); }
}
function CreateError(name, Base) {
    return class extends Base { constructor(...args) { super(...args); this.name = name; } };
}

const contest = { docId: new MockObjectId('0123456789abcdef01234567'), owner: 42 };
const hydroMock = {
    BadRequestError: class extends UserFacingError {},
    ContestModel: {
        async get() { return contest; },
        getMulti(_domain, query) {
            return {
                limit() {
                    return { async toArray() { return [{ ...contest, query }]; } };
                },
            };
        },
    },
    ContestNotLiveError: class extends UserFacingError {},
    ContestNotAttendedError: class extends UserFacingError {},
    CreateError,
    DocumentModel: {},
    DomainModel: {},
    Handler,
    InvalidTokenError: class extends UserFacingError {},
    MessageModel: {},
    ObjectId: MockObjectId,
    PERM: { PERM_EDIT_CONTEST: 1n },
    PermissionError,
    ProblemModel: {},
    ProblemNotFoundError: class extends UserFacingError {},
    RecordModel: {},
    SettingModel: { langs: {} },
    StorageModel: {},
    UserFacingError,
};

const routes = new Map();
const injectedUi = [];
const originalLoad = Module._load;

before(() => {
    Module._load = function load(request, parent, isMain) {
        if (request === 'hydrooj') return hydroMock;
        return originalLoad.call(this, request, parent, isMain);
    };
    require('../index.js').apply({
        Route(_name, route, handler) { routes.set(route, handler); },
        injectUI(...args) { injectedUi.push(args); },
    });
});

test('OJ navigation exposes a teacher-owned contest chooser', async () => {
    assert.equal(injectedUi.length, 1);
    assert.equal(injectedUi[0][0], 'Nav');
    assert.equal(injectedUi[0][1], 'orchestrator_teacher_home');
    const HandlerClass = routes.get('/noi-linux');
    const handler = new HandlerClass();
    handler.user = {
        _id: 42,
        own: () => true,
        hasPerm: (value) => value === 2n,
    };
    handler.response = {};
    hydroMock.PERM.PERM_EDIT_CONTEST_SELF = 2n;
    await handler.get('system');
    assert.equal(handler.response.template, 'orchestrator_teacher_home.html');
    assert.equal(handler.response.body.contests.length, 1);
    assert.deepEqual(handler.response.body.contests[0].query, { owner: 42 });
});

after(() => {
    Module._load = originalLoad;
    fs.rmSync(tempDir, { recursive: true, force: true });
});

test('contest owner receives a short-lived scoped teacher ticket', async () => {
    const HandlerClass = routes.get('/contest/:tid/noi-linux');
    const handler = new HandlerClass();
    handler.user = {
        _id: 42,
        uname: 'coach',
        own: (tdoc) => tdoc.owner === 42,
        hasPerm: () => false,
    };
    handler.response = {};
    await handler.get('system', new MockObjectId('0123456789abcdef01234567'));
    const target = new URL(handler.response.redirect);
    assert.equal(target.origin + target.pathname, 'https://exam.example.test/admin/sso');
    const ticket = target.searchParams.get('ticket');
    const [encoded, signature] = ticket.split('.');
    const expected = crypto.createHmac('sha256', token)
        .update(`noi-teacher-ticket-v1:${encoded}`, 'ascii').digest('base64url');
    assert.equal(signature, expected);
    const payload = JSON.parse(Buffer.from(encoded, 'base64url').toString('utf8'));
    assert.equal(payload.uid, 42);
    assert.equal(payload.uname, 'coach');
    assert.equal(payload.tid, '0123456789abcdef01234567');
    assert.equal(payload.exp - payload.iat, 60);
});

test('ordinary contestant cannot enter the teacher console', async () => {
    const HandlerClass = routes.get('/contest/:tid/noi-linux');
    const handler = new HandlerClass();
    handler.user = {
        _id: 7,
        uname: 'student',
        own: () => false,
        hasPerm: () => false,
    };
    handler.response = {};
    await assert.rejects(
        handler.get('system', new MockObjectId('0123456789abcdef01234567')),
        PermissionError,
    );
});
