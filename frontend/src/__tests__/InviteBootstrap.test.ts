import fs from 'node:fs';
import path from 'node:path';
import { JSDOM } from 'jsdom';

const source = fs.readFileSync(
  path.resolve(process.cwd(), 'public/invite-bootstrap.js'),
  'utf8',
);
const bigboyCompose = fs.readFileSync(
  path.resolve(process.cwd(), '../deploy/bigboy/docker-compose.bigboy.yml'),
  'utf8',
);
const nginxConfig = fs.readFileSync(
  path.resolve(process.cwd(), 'nginx.conf'),
  'utf8',
);
const hostNginxConfig = fs.readFileSync(
  path.resolve(process.cwd(), 'nginx-host.conf'),
  'utf8',
);

function runBootstrap(hostname: string) {
  const secret = 'bootstrap-secret-value-1234567890';
  const dom = new JSDOM(
    '<p id="invite-status">Securing invitation</p>',
    {
      url: `https://${hostname}/invite-bootstrap.html#token=${secret}`,
      runScripts: 'outside-only',
    },
  );
  const instrumented = source.replace(
    'window.location.replace(loginUrl);',
    'window.__capturedInviteLoginUrl = loginUrl;',
  );
  dom.window.eval(instrumented);
  return {
    secret,
    stored: dom.window.sessionStorage.getItem(
      'meetingops.pendingInvitationSecret',
    ),
    browserUrl: dom.window.location.href,
    loginUrl: (dom.window as typeof dom.window & {
      __capturedInviteLoginUrl: string;
    }).__capturedInviteLoginUrl,
  };
}

function exactLocationBlock(config: string, route: string) {
  const start = config.indexOf(`location = ${route} {`);
  expect(start).toBeGreaterThanOrEqual(0);
  const end = config.indexOf('\n    }', start);
  expect(end).toBeGreaterThan(start);
  return config.slice(start, end);
}

describe('public invitation bootstrap', () => {
  it('keeps the secret out of the bigboy oauth2-proxy request', () => {
    const result = runBootstrap('meetingops.magicunicorn.dev');
    expect(result.stored).toBe(result.secret);
    expect(result.browserUrl).toBe(
      'https://meetingops.magicunicorn.dev/invite-bootstrap.html',
    );
    expect(result.loginUrl).toBe(
      '/oauth2/start?rd=%2Fshared%2Fsessions',
    );
    expect(result.loginUrl).not.toContain(result.secret);
  });

  it('uses the token-free native OIDC return path in production', () => {
    const result = runBootstrap('meeting-ops.unicorncommander.ai');
    expect(result.stored).toBe(result.secret);
    expect(result.loginUrl).toBe(
      '/api/auth/sso/uc/start?returnTo=%2Fshared%2Fsessions',
    );
    expect(result.loginUrl).not.toContain(result.secret);
  });

  it('ships only exact public bootstrap routes through bigboy oauth2-proxy', () => {
    expect(bigboyCompose).toContain('^/invite-bootstrap\\.html$$');
    expect(bigboyCompose).toContain('^/invite-bootstrap\\.js$$');
    expect(bigboyCompose).not.toContain('^/invite-bootstrap.*');
  });

  it('keeps public bootstrap responses non-cacheable and explicitly hardened', () => {
    for (const config of [nginxConfig, hostNginxConfig]) {
      const htmlLocation = exactLocationBlock(config, '/invite-bootstrap.html');
      const jsLocation = exactLocationBlock(config, '/invite-bootstrap.js');

      expect(htmlLocation).toContain('add_header X-Frame-Options "DENY" always;');
      expect(htmlLocation).toContain(
        'add_header X-Content-Type-Options "nosniff" always;',
      );
      expect(htmlLocation).toContain(
        'add_header Cache-Control "no-store" always;',
      );
      expect(htmlLocation).toContain(
        'add_header Referrer-Policy "no-referrer" always;',
      );
      expect(htmlLocation).toContain(
        "default-src 'self'; script-src 'self'; style-src 'unsafe-inline'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'",
      );
      expect(jsLocation).toContain(
        'add_header X-Content-Type-Options "nosniff" always;',
      );
      expect(jsLocation).toContain('add_header Cache-Control "no-store" always;');
      expect(jsLocation).toContain(
        'add_header Referrer-Policy "no-referrer" always;',
      );
    }
  });
});
