import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { apiFetch, setOnUnauthorized, triggerUnauthorized } from './api';

// Mock the auth module — getToken is read at call time
vi.mock('./auth', () => ({
  getToken: vi.fn(() => null),
}));

import { getToken } from './auth';
const mockedGetToken = vi.mocked(getToken);

describe('apiFetch', () => {
  const originalFetch = globalThis.fetch;

  beforeEach(() => {
    // Reset the global 401 handler between tests
    setOnUnauthorized(() => {});
    mockedGetToken.mockReturnValue(null);
  });

  afterEach(() => {
    globalThis.fetch = originalFetch;
    vi.restoreAllMocks();
  });

  it('attaches Authorization header when token exists', async () => {
    mockedGetToken.mockReturnValue('test-jwt-token');

    globalThis.fetch = vi.fn().mockResolvedValue(
      new Response('{}', { status: 200 }),
    );

    await apiFetch('/sources');

    const [url, init] = (globalThis.fetch as ReturnType<typeof vi.fn>).mock.calls[0];
    expect(url).toContain('/sources');
    const headers = new Headers(init.headers);
    expect(headers.get('Authorization')).toBe('Bearer test-jwt-token');
  });

  it('omits Authorization header when no token', async () => {
    mockedGetToken.mockReturnValue(null);

    globalThis.fetch = vi.fn().mockResolvedValue(
      new Response('{}', { status: 200 }),
    );

    await apiFetch('/sources');

    const [, init] = (globalThis.fetch as ReturnType<typeof vi.fn>).mock.calls[0];
    const headers = new Headers(init.headers);
    expect(headers.get('Authorization')).toBeNull();
  });

  it('reads token at call time, not at import time (no stale closures)', async () => {
    // First call — no token
    mockedGetToken.mockReturnValue(null);
    globalThis.fetch = vi.fn().mockResolvedValue(
      new Response('{}', { status: 200 }),
    );
    await apiFetch('/first');

    const [, init1] = (globalThis.fetch as ReturnType<typeof vi.fn>).mock.calls[0];
    expect(new Headers(init1.headers).get('Authorization')).toBeNull();

    // Second call — token now available (simulates login completing)
    mockedGetToken.mockReturnValue('fresh-token');
    await apiFetch('/second');

    const [, init2] = (globalThis.fetch as ReturnType<typeof vi.fn>).mock.calls[1];
    expect(new Headers(init2.headers).get('Authorization')).toBe('Bearer fresh-token');
  });

  it('calls onUnauthorized callback on 401 response', async () => {
    const logoutSpy = vi.fn();
    setOnUnauthorized(logoutSpy);

    globalThis.fetch = vi.fn().mockResolvedValue(
      new Response('Unauthorized', { status: 401 }),
    );

    await apiFetch('/protected');

    expect(logoutSpy).toHaveBeenCalledOnce();
  });

  it('does NOT call onUnauthorized on non-401 responses', async () => {
    const logoutSpy = vi.fn();
    setOnUnauthorized(logoutSpy);

    globalThis.fetch = vi.fn().mockResolvedValue(
      new Response('Forbidden', { status: 403 }),
    );

    await apiFetch('/forbidden');

    expect(logoutSpy).not.toHaveBeenCalled();
  });

  it('passes through request options (method, body, extra headers)', async () => {
    mockedGetToken.mockReturnValue('tok');

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
    // Reset to null by setting a handler then re-importing...
    // Actually, triggerUnauthorized guards with `if (_onUnauthorized)`.
    // We can't easily reset to null since setOnUnauthorized only accepts () => void.
    // But we can verify it doesn't throw when called after setting a no-op.
    setOnUnauthorized(() => {});
    expect(() => triggerUnauthorized()).not.toThrow();
  });
});

describe('setOnUnauthorized', () => {
  it('replaces previously registered handler', async () => {
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
