const assert = require('node:assert/strict');
const crypto = require('node:crypto');
const fs = require('node:fs');
const Module = require('node:module');
const os = require('node:os');
const path = require('node:path');
const { after, before, test } = require('node:test');

const tempDir = fs.mkdtempSync(path.join(os.tmpdir(), 'hydro-orchestrator-notify-test-'));
process.env.ORCHESTRATOR_TOKEN = 'test-token-that-is-at-least-thirty-two-characters';
process.env.ORCHESTRATOR_IDEMPOTENCY_FILE = path.join(tempDir, 'submissions.json');
process.env.ORCHESTRATOR_NOTIFICATION_IDEMPOTENCY_FILE = path.join(
    tempDir,
    'notifications.json',
);
process.env.ORCHESTRATOR_PROBLEM_DRAFT_IDEMPOTENCY_FILE = path.join(
    tempDir,
    'problem-drafts.json',
);
process.env.ORCHESTRATOR_NOTIFY_ALLOWED_HTTPS_HOSTS = 'exam.example.test, exam-backup.example.test';

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

const messages = [];
let sendCalls = 0;
const MessageModel = {
    FLAG_UNREAD: 1,
    FLAG_RICHTEXT: 4,
    coll: {
        async findOne(query) {
            return messages.find((item) => (
                item.from === query.from
                && item.to.includes(query.to)
                && item.content === query.content
            )) || null;
        },
    },
    async send(from, to, content, flag) {
        sendCalls += 1;
        const document = {
            _id: new MockObjectId(String(sendCalls).padStart(24, '0')),
            from,
            to: Array.isArray(to) ? to : [to],
            content,
            flag,
        };
        messages.push(document);
        return document;
    },
};

const hydroMock = {
    BadRequestError,
    ContestModel: {},
    ContestNotLiveError: class extends UserFacingError {},
    ContestNotAttendedError: class extends UserFacingError {},
    CreateError,
    DomainModel: {},
    Handler,
    InvalidTokenError,
    MessageModel,
    ObjectId: MockObjectId,
    ProblemModel: {},
    ProblemNotFoundError: class extends UserFacingError {},
    RecordModel: {},
    SettingModel: { langs: {} },
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

after(() => {
    Module._load = originalLoad;
    fs.rmSync(tempDir, { recursive: true, force: true });
});

function notificationId(value) {
    return crypto.createHash('sha256').update(value).digest('hex');
}

async function notify(overrides = {}, headers = {}) {
    const handler = new routes.orchestrator_notify.handler();
    handler.request = {
        headers: {
            'x-orchestrator-token': process.env.ORCHESTRATOR_TOKEN,
            ...headers,
        },
        body: {
            notification_id: notificationId('student-7-seat-v1'),
            purpose: 'seat_ready',
            uid: 7,
            contest_title: 'CSP-J 模拟赛',
            desktop_url: 'https://exam.example.test/s/personal-seat/vnc.html?autoconnect=true',
            candidate: 'CSPJ-0007',
            student_password: 'student-only-secret',
            available_at: '2026-08-08 13:55',
            ...overrides,
        },
    };
    handler.response = {};
    await handler.post();
    return handler.response.body;
}

test('registers notify below the already private submit URL prefix', () => {
    assert.equal(routes.orchestrator_notify.route, '/orchestrator/submit/notify');
});

test('requires the shared internal token', async () => {
    await assert.rejects(
        notify({}, { 'x-orchestrator-token': '' }),
        (error) => error instanceof InvalidTokenError && error.code === 403,
    );
    assert.equal(sendCalls, 0);
});

test('sends a native UID 1 structured unread rich-text message', async () => {
    const result = await notify();

    assert.equal(result.notification_id, notificationId('student-7-seat-v1'));
    assert.equal(result.message_id, '000000000000000000000001');
    assert.equal(sendCalls, 1);
    const sent = messages[0];
    assert.equal(sent.from, 1);
    assert.deepEqual(sent.to, [7]);
    assert.equal(sent.flag, MessageModel.FLAG_RICHTEXT | MessageModel.FLAG_UNREAD);
    const structured = JSON.parse(sent.content);
    assert.deepEqual(Object.keys(structured).sort(), ['message', 'params']);
    assert.match(structured.message, /\{desktop:link\}/);
    assert.doesNotMatch(structured.message, /\[[^\]]+\]\([^)]+\)|^#{1,6}\s/m);
    assert.equal(structured.params.desktop, 'https://exam.example.test/s/personal-seat/vnc.html?autoconnect=true');
    assert.equal(structured.params.candidate, 'CSPJ-0007');
    assert.equal(structured.params.password, 'student-only-secret');
    assert.equal(structured.params._notification_id, result.notification_id);
});

test('returns the original message for an identical retry', async () => {
    const result = await notify();
    assert.equal(result.message_id, '000000000000000000000001');
    assert.equal(sendCalls, 1);
});

test('rejects reuse of a notification id with changed credentials', async () => {
    await assert.rejects(
        notify({ student_password: 'a-different-student-password' }),
        (error) => error.code === 409,
    );
    assert.equal(sendCalls, 1);
});

test('recovers an inserted native message after a plugin restart', async () => {
    const state = JSON.parse(fs.readFileSync(
        process.env.ORCHESTRATOR_NOTIFICATION_IDEMPOTENCY_FILE,
        'utf8',
    ));
    state.entries[notificationId('student-7-seat-v1')].complete = false;
    state.entries[notificationId('student-7-seat-v1')].messageId = '';
    fs.writeFileSync(
        process.env.ORCHESTRATOR_NOTIFICATION_IDEMPOTENCY_FILE,
        `${JSON.stringify(state)}\n`,
    );
    delete require.cache[require.resolve('../index.js')];
    applyPlugin();

    const result = await notify();
    assert.equal(result.message_id, '000000000000000000000001');
    assert.equal(sendCalls, 1);
});

test('only accepts HTTPS links on explicitly configured exam hosts', async () => {
    const invalidUrls = [
        'http://exam.example.test/s/seat',
        'https://oj.example.test/s/seat',
        'https://exam.example.test.evil.example/s/seat',
        'https://root:secret@exam.example.test/s/seat',
        'https://exam.example.test:8443/s/seat',
        'https://exam.example.test/s/seat#secret',
    ];
    for (const [index, desktopUrl] of invalidUrls.entries()) {
        await assert.rejects(
            notify({
                notification_id: notificationId(`invalid-url-${index}`),
                desktop_url: desktopUrl,
            }),
            (error) => error instanceof BadRequestError && error.code === 400,
        );
    }
    assert.equal(sendCalls, 1);
});

test('rejects arbitrary secret fields and UID 1 as recipient', async () => {
    await assert.rejects(
        notify({
            notification_id: notificationId('ssh-secret'),
            ssh_password: 'must-never-be-sent',
        }),
        (error) => error instanceof BadRequestError && error.code === 400,
    );
    await assert.rejects(
        notify({
            notification_id: notificationId('admin-secret'),
            admin_password: 'must-never-be-sent',
        }),
        (error) => error instanceof BadRequestError && error.code === 400,
    );
    await assert.rejects(
        notify({ notification_id: notificationId('uid-one'), uid: 1 }),
        (error) => error instanceof BadRequestError && error.code === 400,
    );
    assert.equal(sendCalls, 1);
});
