import { http, HttpResponse } from 'msw';

export const loginHandler = http.post(
  '/api/v1/auth/login',
  async ({ request }) => {
    const body = (await request.json()) as {
      username?: string;
      password?: string;
    };
    if (body.username === 'admin' && body.password === 'rsi-admin') {
      return HttpResponse.json({
        token: 'test-token',
        expires_at: 1893456000,
        user_id: 'u-admin',
        role: 'admin',
      });
    }
    return HttpResponse.json({ detail: 'invalid credentials' }, { status: 401 });
  },
);

export const meHandler = http.get('/api/v1/auth/me', () =>
  HttpResponse.json({
    user_id: 'u-admin',
    role: 'admin',
    expires_at: 1893456000,
  }),
);

export const handlers = [loginHandler, meHandler];
