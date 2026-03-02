import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { apiFetch, setOnUnauthorized, triggerUnauthorized } from './api';

// Mock the auth module — getToken and isTokenExpired are read at call time
vi.mock('./auth', () => ({
  getToken: vi.fn(() => null),
  isTokenExpired: vi.fn(() => false),
}));

import { getToken, isTokenExpired } from './auth';
const mockedGetToken = vi.mocked(getToken);
const mockedIsTokenExpired = vi.mocked(isTokenExpired);

// Helper: set up a valid token (not expired)
function mockValidToken(token = 'test-jwt-token') {
  mockedGetToken.mockReturnValue(token);
  mockedIsTokenExpired.mockReturnValue(false);
}

describe('apiFetch', () => {
  const originalFetch = globalThis.fetch;

  beforeEach(() => {
    setOnUnauthorized(() => {});
    mockedGetToken.mockReturnValue(null);
    mockedIsTokenExpired.mockReturnValue(false);
  });

  afterEach(() => {
    globalThis.fetch = originalFetch;
    vi.restoreAllMocks();
  });

  it('attaches Authorization header when token exists and is valid', async () => {
    mockValidToken('test-jwt-token');

    globalThis.fetch = vi.fn().mockResolvedValue(
      new Response('{}', { status: 200 }),
    );

    await apiFetch('/sources');

    const [url, init] = (globalThis.fetch as ReturnType<typeof vi.fn>).mock.calls[0];
    expect(url).toContain('/sources');
    const headers = new Headers(init.headers);
    expect(headers.get('Authorization')).toBe('Bearer test-jwt-token');
  });

  it('returns 401 locally when no token — does NOT call fetch', async () => {
    mockedGetToken.mockReturnValue(null);
    const logoutSpy = vi.fn();
    setOnUnauthorized(logoutSpy);

    globalThis.fetch = vi.fn();

    const res = await apiFetch('/sources');

    expect(res.status).toBe(401);
    expect(logoutSpy).toHaveBeenCalledOnce();
    expect(globalThis.fetch).not.toHaveBeenCalled();
  });

  it('returns 401 locally when token is expired — does NOT call fetch', async () => {
    mockedGetToken.mockReturnValue('expired-token');
    mockedIsTokenExpired.mockReturnValue(true);
    const logoutSpy = vi.fn();
    setOnUnauthorized(logoutSpy);

    globalThis.fetch = vi.fn();

    const res = await apiFetch('/protected');

    expect(res.status).toBe(401);
    expect(logoutSpy).toHaveBeenCalledOnce();
    expect(globalThis.fetch).not.toHaveBeenCalled();
  });

  it('reads token at call time, not at import time (no stale closures)', async () => {
    // First call — no token → local 401
    mockedGetToken.mockReturnValue(null);
    globalThis.fetch = vi.fn().mockResolvedValue(
      new Response('{}', { status: 200 }),
    );
    const res1 = await apiFetch('/first');
    expect(res1.status).toBe(401);
    expect(globalThis.fetch).not.toHaveBeenCalled();

    // Second call — token now available (simulates login completing)
    mockValidToken('fresh-token');
    await apiFetch('/second');

    expect(globalThis.fetch).toHaveBeenCalledOnce();
    const [, init2] = (globalThis.fetch as ReturnType<typeof vi.fn>).mock.calls[0];
    expect(new Headers(init2.headers).get('Authorization')).toBe('Bearer fresh-token');
  });

  it('calls onUnauthorized callback on 401 response from server', async () => {
    mockValidToken();
    const logoutSpy = vi.fn();
    setOnUnauthorized(logoutSpy);

    globalThis.fetch = vi.fn().mockResolvedValue(
      new Response('Unauthorized', { status: 401 }),
    );

    await apiFetch('/protected');

    expect(logoutSpy).toHaveBeenCalledOnce();
  });

  it('does NOT call onUnauthorized on non-401 responses', async () => {
    mockValidToken();
    const logoutSpy = vi.fn();
    setOnUnauthorized(logoutSpy);

    globalThis.fetch = vi.fn().mockResolvedValue(
      new Response('Forbidden', { status: 403 }),
    );

    await apiFetch('/forbidden');

    expect(logoutSpy).not.toHaveBeenCalled();
  });

  it('passes through request options (method, body, extra headers)', async () => {
    mockValidToken('tok');

    globalThis.fetch = vi.fn().mockResolvedValue(
      new Response('{}', { status: 200 }),
    );

    await apiFetch('/chat_id', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ chat_id: 'abc' }),
    });

    const [, init] = (globalThis.fetch as ReturnType<typeof vi.fn>).mock.calls[0];
    expect(init.method).toBe('POST');
    expect(init.body).toBe('{"chat_id":"abc"}');
    const headers = new Headers(init.headers);
    expect(headers.get('Content-Type')).toBe('application/json');
    expect(headers.get('Authorization')).toBe('Bearer tok');
  });

  it('constructs full URL from path', async () => {
    mockValidToken();
    globalThis.fetch = vi.fn().mockResolvedValue(
      new Response('{}', { status: 200 }),
    );

    await apiFetch('/sources');

    const [url] = (globalThis.fetch as ReturnType<typeof vi.fn>).mock.calls[0];
    // In jsdom, window.location.hostname is 'localhost'
    expect(url).toMatch(/^https?:\/\/.+\/sources$/);
  });
});

describe('triggerUnauthorized', () => {
  it('calls the registered handler', () => {
    const spy = vi.fn();
    setOnUnauthorized(spy);

    triggerUnauthorized();

    expect(spy).toHaveBeenCalledOnce();
  });

  it('does nothing when no handler registered', () => {
    setOnUnauthorized(() => {});
    expect(() => triggerUnauthorized()).not.toThrow();
  });
});

describe('setOnUnauthorized', () => {
  it('replaces previously registered handler', async () => {
    mockValidToken();
    const first = vi.fn();
    const second = vi.fn();

    setOnUnauthorized(first);
    setOnUnauthorized(second);

    globalThis.fetch = vi.fn().mockResolvedValue(
      new Response('', { status: 401 }),
    );

    await apiFetch('/test');

    expect(first).not.toHaveBeenCalled();
    expect(second).toHaveBeenCalledOnce();
  });
});
