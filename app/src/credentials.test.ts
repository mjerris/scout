import { scoutCredentials } from './credentials';

test('uses the first token once, then asks Scout; expiry in milliseconds', async () => {
  const token = jest.fn(async () => ({ token: 'fresh', expires_at: 1760007200 }));
  const creds = scoutCredentials({ token }, { token: 'first', expires_at: 1760003600 });
  expect(await creds.authenticate()).toEqual({ token: 'first', expiry_at: 1760003600000 });
  expect(token).not.toHaveBeenCalled();
  expect(await creds.authenticate()).toEqual({ token: 'fresh', expiry_at: 1760007200000 });
  expect(await creds.refresh!()).toEqual({ token: 'fresh', expiry_at: 1760007200000 });
  expect(token).toHaveBeenCalledTimes(2);
});
