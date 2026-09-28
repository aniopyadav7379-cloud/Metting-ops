import { describe, it, expect, vi, afterEach } from 'vitest';
import { config } from '../config';

const setLocation = (url: string) => {
  const parsed = new URL(url);
  const locationMock = {
    href: parsed.href,
    hostname: parsed.hostname,
    protocol: parsed.protocol,
  };

  vi.stubGlobal('location', locationMock);

  try {
    Object.defineProperty(window, 'location', {
      value: locationMock,
      writable: true,
    });
  } catch (error) {
    // Ignore if jsdom prevents redefining window.location
  }
};

describe('config api urls', () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it('has apiUrl property', () => {
    setLocation('http://localhost:7777');
    expect(config.apiUrl).toBeDefined();
    expect(typeof config.apiUrl).toBe('string');
  });

  it('has apiBaseUrl property', () => {
    setLocation('http://localhost:7777');
    expect(config.apiBaseUrl).toBeDefined();
    expect(typeof config.apiBaseUrl).toBe('string');
    // apiBaseUrl and apiUrl should return the same value
    expect(config.apiBaseUrl).toBe(config.apiUrl);
  });

  it('uses localhost backend for local development', () => {
    setLocation('http://localhost:7777');
    expect(config.apiUrl).toBe('http://localhost:9050');
  });

  it('uses dev backend port when running under Vite dev/test', () => {
    setLocation('https://app.example.com');
    expect(config.apiUrl).toBe('http://app.example.com:9050');
  });
});
