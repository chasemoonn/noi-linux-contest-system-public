const assert = require('node:assert/strict');
const crypto = require('node:crypto');
const fs = require('node:fs');
const Module = require('node:module');
const os = require('node:os');
const path = require('node:path');
const { after, before, test } = require('node:test');

const tempDir = fs.mkdtempSync(path.join(os.tmpdir(), 'hydro-orchestrator-test-'));
process.env.ORCHESTRATOR_TOKEN = 'test-token-that-is-at-least-thirty-two-characters';
process.env.ORCHESTRATOR_IDEMPOTENCY_FILE = path.join(tempDir, 'idempotency.json');
process.env.ORCHESTRATOR_NOTIFICATION_IDEMPOTENCY_FILE = path.join(
    tempDir,
    'notifications.json',
);
process.env.ORCHESTRATOR_PROBLEM_DRAFT_IDEMPOTENCY_FILE = path.join(
    tempDir,
    'problem-drafts.json',
);

const calls = {
    recordAdds: 0,
    statusUpdates: 0,
    problemCounts: 0,
    userCounts: 0,
    ongoingChecks: 0,
};

let contestOngoing = true;
const defaultContest = {
    pids: [101],
    rule: 'oi',
    beginAt: new Date('2026-08-07T00:00:00Z'),
    endAt: new Date('2026-08-08T00:00:00Z'),
};
const defaultContestStatus = { attended: true };
let contestOverrides = {};
let contestStatusOverrides = {};
let contestStatusPresent = true;
let recordAddBarrier = null;

function deferred() {
    let resolve;
    const promise = new Promise((done) => { resolve = done; });
    return { promise, resolve };
}

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
class ContestNotLiveError extends InvalidTokenError {}
class ContestNotAttendedError extends InvalidTokenError {}
class ProblemNotFoundError extends Error {}
class Handler {}

const hydroMock = {
    BadRequestError,
    ContestModel: {
        get: async (_domain, tid) => ({
            ...defaultContest,
            ...contestOverrides,
            docId: tid,
        }),
        getStatus: async () => (contestStatusPresent ? ({
            ...defaultContestStatus,
            ...contestStatusOverrides,
        }) : null),
        isOngoing: () => {
            calls.ongoingChecks += 1;
            return contestOngoing;
        },
        updateStatus: async () => { calls.statusUpdates += 1; },
    },
    ContestNotLiveError,
    ContestNotAttendedError,
    CreateError,
    DomainModel: {
        incUserInDomain: async () => { calls.userCounts += 1; },
    },
    Handler,
    InvalidTokenError,
    ObjectId: MockObjectId,
    ProblemModel: {
        get: async () => ({ docId: 101, config: { langs: ['cc'] } }),
        inc: async () => { calls.problemCounts += 1; },
    },
    ProblemNotFoundError,
    RecordModel: {
        add: async () => {
            calls.recordAdds += 1;
            const rid = new MockObjectId(String(calls.recordAdds).padStart(24, '0'));
            if (recordAddBarrier) {
                recordAddBarrier.started.resolve();
                await recordAddBarrier.release.promise;
            }
            return rid;
        },
    },
    SettingModel: { langs: { cc: { disabled: false } } },
    UserFacingError,
};

let routeHandler;
const originalLoad = Module._load;

before(() => {
    Module._load = function load(request, parent, isMain) {
        if (request === 'hydrooj') return hydroMock;
        return originalLoad.call(this, request, parent, isMain);
    };
    const plugin = require('../index.js');
    plugin.apply({
        Route(_name, _route, handler) {
            routeHandler = handler;
        },
    });
});

after(() => {
    Module._load = originalLoad;
    fs.rmSync(tempDir, { recursive: true, force: true });
});

function submissionId(value) {
    return crypto.createHash('sha256').update(value).digest('hex');
}

function defaultFingerprint() {
    return crypto.createHash('sha256')
        .update(JSON.stringify(['0'.repeat(24), 7, 'P1', 'cc', 'int main() {}']))
        .digest('hex');
}

async function submit(overrides = {}) {
    const handler = new routeHandler();
    handler.request = {
        headers: { 'x-orchestrator-token': process.env.ORCHESTRATOR_TOKEN },
        body: {
            tid: '0'.repeat(24),
            uid: 7,
            pid: 'P1',
            code: 'int main() {}',
            lang: 'cc',
            submission_id: submissionId('default'),
            ...overrides,
        },
    };
    handler.response = {};
    await handler.post();
    return handler.response.body;
}

test('rejects a missing token', async () => {
    const handler = new routeHandler();
    handler.request = { headers: {}, body: {} };
    handler.response = {};
    await assert.rejects(handler.post(), (error) => {
        assert.ok(error instanceof InvalidTokenError);
        assert.equal(error.code, 403);
        return true;
    });
});

test('reports an invalid payload as bad request', async () => {
    const before = calls.recordAdds;
    await assert.rejects(
        submit({ submission_kind: 'unexpected' }),
        (error) => {
            assert.ok(error instanceof BadRequestError);
            assert.equal(error.code, 400);
            return true;
        },
    );
    assert.equal(calls.recordAdds, before);
});

test('reports oversized source code as payload too large', async () => {
    const before = calls.recordAdds;
    await assert.rejects(
        submit({
            code: 'x'.repeat(512 * 1024 + 1),
            submission_id: submissionId('oversized'),
        }),
        (error) => {
            assert.equal(error.code, 413);
            return true;
        },
    );
    assert.equal(calls.recordAdds, before);
});

test('persists one record and returns its rid', async () => {
    const result = await submit();
    assert.equal(result.rid, '000000000000000000000001');
    assert.equal(calls.recordAdds, 1);
    assert.equal(calls.statusUpdates, 1);
    assert.equal(calls.problemCounts, 1);
    assert.equal(calls.userCounts, 1);
    const persisted = JSON.parse(
        fs.readFileSync(process.env.ORCHESTRATOR_IDEMPOTENCY_FILE, 'utf8'),
    ).entries[submissionId('default')];
    assert.equal(persisted.contestDocId, '0'.repeat(24));
    assert.equal(persisted.problemDocId, 101);
    assert.equal(persisted.uid, 7);
    if (process.platform !== 'win32') {
        assert.equal(
            fs.statSync(process.env.ORCHESTRATOR_IDEMPOTENCY_FILE).mode & 0o777,
            0o600,
        );
    }
});

test('returns the same rid for an identical retry without a second record', async () => {
    const result = await submit();
    assert.equal(result.rid, '000000000000000000000001');
    assert.equal(calls.recordAdds, 1);
    assert.equal(calls.statusUpdates, 1);
    assert.equal(calls.problemCounts, 1);
    assert.equal(calls.userCounts, 1);
});

test('does not include submission kind in the legacy idempotency fingerprint', async () => {
    const before = calls.recordAdds;
    const result = await submit({ submission_kind: 'realtime' });

    assert.equal(result.rid, '000000000000000000000001');
    assert.equal(calls.recordAdds, before);
});

test('returns the persisted rid after a plugin restart', async () => {
    delete require.cache[require.resolve('../index.js')];
    const restarted = require('../index.js');
    restarted.apply({
        Route(_name, _route, handler) {
            routeHandler = handler;
        },
    });

    const result = await submit();
    assert.equal(result.rid, '000000000000000000000001');
    assert.equal(calls.recordAdds, 1);
    assert.equal(calls.statusUpdates, 1);
    assert.equal(calls.problemCounts, 1);
    assert.equal(calls.userCounts, 1);
});

test('restart resumes an incomplete legacy journal without re-adding the record', async () => {
    const id = submissionId('incomplete-restart');
    const rid = 'f'.repeat(24);
    const state = JSON.parse(
        fs.readFileSync(process.env.ORCHESTRATOR_IDEMPOTENCY_FILE, 'utf8'),
    );
    state.entries[id] = {
        fingerprint: defaultFingerprint(),
        rid,
        statusUpdated: false,
        problemCounted: false,
        userCounted: false,
        complete: false,
        createdAt: new Date().toISOString(),
    };
    fs.writeFileSync(
        process.env.ORCHESTRATOR_IDEMPOTENCY_FILE,
        `${JSON.stringify(state)}\n`,
        'utf8',
    );

    delete require.cache[require.resolve('../index.js')];
    const restarted = require('../index.js');
    restarted.apply({
        Route(_name, _route, handler) {
            routeHandler = handler;
        },
    });

    const before = { ...calls };
    contestOngoing = false;
    contestStatusPresent = false;
    contestOverrides = { pids: [], rule: 'ioi' };
    hydroMock.SettingModel.langs.cc.disabled = true;
    try {
        const result = await submit({ submission_id: id, submission_kind: 'realtime' });
        assert.equal(result.rid, rid);
    } finally {
        contestOngoing = true;
        contestStatusPresent = true;
        contestOverrides = {};
        hydroMock.SettingModel.langs.cc.disabled = false;
    }

    assert.equal(calls.recordAdds, before.recordAdds);
    assert.equal(calls.statusUpdates, before.statusUpdates + 1);
    assert.equal(calls.problemCounts, before.problemCounts + 1);
    assert.equal(calls.userCounts, before.userCounts + 1);
    const recovered = JSON.parse(
        fs.readFileSync(process.env.ORCHESTRATOR_IDEMPOTENCY_FILE, 'utf8'),
    ).entries[id];
    assert.equal(recovered.complete, true);
    assert.equal(recovered.contestDocId, '0'.repeat(24));
    assert.equal(recovered.problemDocId, 101);
    assert.equal(recovered.uid, 7);
});

test('replays a persisted rid before all mutable contest validations', async () => {
    const request = {
        submission_id: submissionId('persisted-after-state-changes'),
        submission_kind: 'realtime',
    };
    const first = await submit(request);
    const before = { ...calls };

    try {
        contestOngoing = false;
        assert.equal((await submit(request)).rid, first.rid, 'contest ended');

        contestOngoing = true;
        contestStatusPresent = false;
        assert.equal((await submit(request)).rid, first.rid, 'student withdrew');

        contestStatusPresent = true;
        contestOverrides = { pids: [] };
        assert.equal((await submit(request)).rid, first.rid, 'problem removed');

        contestOverrides = { rule: 'ioi' };
        assert.equal((await submit(request)).rid, first.rid, 'contest rule changed');

        contestOverrides = {};
        hydroMock.SettingModel.langs.cc.disabled = true;
        assert.equal((await submit(request)).rid, first.rid, 'language disabled');

        contestStatusPresent = false;
        contestOverrides = { pids: [], rule: 'ioi' };
        await assert.rejects(
            submit({ ...request, code: 'int main() { return 1; }' }),
            (error) => {
                assert.equal(error.code, 409);
                return true;
            },
        );
    } finally {
        contestOngoing = true;
        contestStatusPresent = true;
        contestOverrides = {};
        hydroMock.SettingModel.langs.cc.disabled = false;
    }

    assert.deepEqual(calls, before);
});

test('waits for an in-flight rid before mutable validation and still conflicts on changes', async () => {
    const request = {
        submission_id: submissionId('in-flight-after-state-changes'),
        submission_kind: 'realtime',
    };
    const beforeAdds = calls.recordAdds;
    recordAddBarrier = { started: deferred(), release: deferred() };
    const firstPromise = submit(request);
    await recordAddBarrier.started.promise;

    contestOngoing = false;
    contestStatusPresent = false;
    contestOverrides = { pids: [], rule: 'ioi' };
    hydroMock.SettingModel.langs.cc.disabled = true;
    const replayPromise = submit(request);

    try {
        await assert.rejects(
            submit({ ...request, code: 'int main() { return 2; }' }),
            (error) => {
                assert.equal(error.code, 409);
                return true;
            },
        );
    } finally {
        contestOngoing = true;
        contestStatusPresent = true;
        contestOverrides = {};
        hydroMock.SettingModel.langs.cc.disabled = false;
        recordAddBarrier.release.resolve();
    }

    const [first, replay] = await Promise.all([firstPromise, replayPromise]);
    recordAddBarrier = null;
    assert.equal(replay.rid, first.rid);
    assert.equal(calls.recordAdds, beforeAdds + 1);
});

test('rejects reuse of an idempotency key with changed source', async () => {
    const before = calls.recordAdds;
    await assert.rejects(
        submit({ code: 'int main() { return 1; }' }),
        (error) => {
            assert.equal(error.code, 409);
            return true;
        },
    );
    assert.equal(calls.recordAdds, before);
});

test('coalesces concurrent identical submissions', async () => {
    const id = submissionId('concurrent');
    const before = calls.recordAdds;
    const [left, right] = await Promise.all([
        submit({ submission_id: id }),
        submit({ submission_id: id }),
    ]);
    assert.equal(left.rid, right.rid);
    assert.equal(calls.recordAdds, before + 1);
});

test('creates distinct records for distinct submission ids', async () => {
    const before = calls.recordAdds;
    const left = await submit({ submission_id: submissionId('attempt-left') });
    const right = await submit({ submission_id: submissionId('attempt-right') });

    assert.notEqual(left.rid, right.rid);
    assert.equal(calls.recordAdds, before + 2);
});

test('rejects a realtime submission outside the contest window', async () => {
    contestOngoing = false;
    const before = calls.recordAdds;
    try {
        await assert.rejects(
            submit({
                submission_id: submissionId('realtime-after-contest'),
                submission_kind: 'realtime',
            }),
            ContestNotLiveError,
        );
    } finally {
        contestOngoing = true;
    }
    assert.equal(calls.recordAdds, before);
});

test('accepts a realtime action accepted before cutoff even if delivered later', async () => {
    contestOngoing = false;
    const beforeAdds = calls.recordAdds;
    const beforeChecks = calls.ongoingChecks;
    try {
        const result = await submit({
            submission_id: submissionId('accepted-before-delivered-after'),
            submission_kind: 'realtime',
            accepted_at_ms: defaultContest.endAt.getTime() - 1,
        });
        assert.ok(result.rid);
    } finally {
        contestOngoing = true;
    }
    assert.equal(calls.recordAdds, beforeAdds + 1);
    assert.equal(calls.ongoingChecks, beforeChecks);
});

test('rejects a realtime action at or after the personal endAt', async () => {
    contestStatusOverrides = { endAt: new Date('2026-08-07T12:00:00Z') };
    const before = calls.recordAdds;
    try {
        await assert.rejects(
            submit({
                submission_id: submissionId('personal-end-at'),
                submission_kind: 'realtime',
                accepted_at_ms: contestStatusOverrides.endAt.getTime(),
            }),
            ContestNotLiveError,
        );
    } finally {
        contestStatusOverrides = {};
    }
    assert.equal(calls.recordAdds, before);
});

test('rejects a realtime action at or after the duration deadline', async () => {
    contestOverrides = { duration: 2 };
    contestStatusOverrides = { startAt: new Date('2026-08-07T06:00:00Z') };
    const before = calls.recordAdds;
    try {
        await assert.rejects(
            submit({
                submission_id: submissionId('duration-end-at'),
                submission_kind: 'realtime',
                accepted_at_ms: new Date('2026-08-07T08:00:00Z').getTime(),
            }),
            ContestNotLiveError,
        );
    } finally {
        contestOverrides = {};
        contestStatusOverrides = {};
    }
    assert.equal(calls.recordAdds, before);
});

test('rejects a realtime action accepted before the personal startAt', async () => {
    contestStatusOverrides = { startAt: new Date('2026-08-07T06:00:00Z') };
    const before = calls.recordAdds;
    try {
        await assert.rejects(
            submit({
                submission_id: submissionId('personal-start-at'),
                submission_kind: 'realtime',
                accepted_at_ms: new Date('2026-08-07T05:59:59Z').getTime(),
            }),
            ContestNotLiveError,
        );
    } finally {
        contestStatusOverrides = {};
    }
    assert.equal(calls.recordAdds, before);
});

test('rejects realtime submissions for a non-OI contest', async () => {
    contestOverrides = { rule: 'ioi' };
    const before = calls.recordAdds;
    try {
        await assert.rejects(
            submit({
                submission_id: submissionId('non-oi-realtime'),
                submission_kind: 'realtime',
                accepted_at_ms: new Date('2026-08-07T12:00:00Z').getTime(),
            }),
            (error) => {
                assert.ok(error instanceof BadRequestError);
                assert.equal(error.code, 400);
                return true;
            },
        );
    } finally {
        contestOverrides = {};
    }
    assert.equal(calls.recordAdds, before);
});

test('returns an existing realtime rid after the contest window closes', async () => {
    contestOngoing = false;
    const beforeAdds = calls.recordAdds;
    const beforeChecks = calls.ongoingChecks;
    try {
        const result = await submit({
            submission_kind: 'realtime',
            accepted_at_ms: defaultContest.endAt.getTime() + 1,
        });
        assert.equal(result.rid, '000000000000000000000001');
    } finally {
        contestOngoing = true;
    }
    assert.equal(calls.recordAdds, beforeAdds);
    assert.equal(calls.ongoingChecks, beforeChecks);
});

test('allows a final submission outside the contest window', async () => {
    contestOngoing = false;
    const beforeAdds = calls.recordAdds;
    const beforeChecks = calls.ongoingChecks;
    try {
        const result = await submit({
            submission_id: submissionId('final-after-contest'),
            submission_kind: 'final',
        });
        assert.ok(result.rid);
    } finally {
        contestOngoing = true;
    }
    assert.equal(calls.recordAdds, beforeAdds + 1);
    assert.equal(calls.ongoingChecks, beforeChecks);
});
