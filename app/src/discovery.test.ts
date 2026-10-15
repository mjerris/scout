import { foundFromService } from './discovery';

test("a Bonjour record gives Scout's https address and a name to show", () => {
  expect(
    foundFromService({
      name: "Scout on Mike's Mac",
      host: 'mikes-mac.local.',
      txt: { url: 'https://mac.tail1.ts.net', host: "Mike's Mac" },
    }),
  ).toEqual({ key: "Scout on Mike's Mac", label: "Mike's Mac", server: 'https://mac.tail1.ts.net' });
});

test('records without an https address are left out', () => {
  expect(foundFromService({ name: 'Scout on x', txt: { url: 'http://192.168.1.5:8765' } })).toBeNull();
  expect(foundFromService({ name: 'Scout on x', txt: {} })).toBeNull();
  expect(foundFromService({ name: 'Scout on x' })).toBeNull();
  expect(foundFromService(null)).toBeNull();
});

test('without the label, the name of the address is shown', () => {
  expect(foundFromService({ name: 'Scout', txt: { url: 'https://mac.tail1.ts.net' } })?.label).toBe('mac.tail1.ts.net');
});
