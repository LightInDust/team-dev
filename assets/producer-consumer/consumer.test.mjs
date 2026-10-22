import assert from 'node:assert/strict';
import fs from 'node:fs';
import test from 'node:test';
import {renderPreview} from './consumer.mjs';

// No fallback to the committed fixture: acceptance must supply fresh JSON.
const fixturePath = process.argv[2];
assert.ok(fixturePath, 'A generated fixture path is required');
const responses = JSON.parse(fs.readFileSync(fixturePath, 'utf8'));

function render(payload) {
  const view = {count: {textContent: 'unchanged'}, confirm: {disabled: false}};
  renderPreview(payload, view);
  return view;
}

test('producer supplies all seeded cases', () => {
  assert.ok(Array.isArray(responses));
  assert.equal(responses.length, 3);
});

for (const [index, expected] of [0, 1, 42].entries()) {
  test(`serialized ${expected} rows drive visible state and confirmation`, () => {
    const payload = responses[index];
    assert.equal(payload.station_count, expected);
    const view = render(payload);
    assert.equal(view.count.textContent, expected === 0 ? 'No stations' : `${expected} stations`);
    assert.equal(view.confirm.disabled, expected === 0);
  });
}

test('missing field cannot silently enable confirmation', () => {
  const payload = {...responses[1]};
  delete payload.station_count;
  const view = render(payload);
  assert.equal(view.count.textContent, 'Invalid preview');
  assert.equal(view.confirm.disabled, true);
});

test('mistaken field name cannot silently enable confirmation', () => {
  const payload = {...responses[1], chosen_station_count: responses[1].station_count};
  delete payload.station_count;
  const view = render(payload);
  assert.equal(view.count.textContent, 'Invalid preview');
  assert.equal(view.confirm.disabled, true);
});

for (const value of ['1', null, true, -1, 1.5]) {
  test(`invalid count ${JSON.stringify(value)} is rejected`, () => {
    const view = render({...responses[1], station_count: value});
    assert.equal(view.count.textContent, 'Invalid preview');
    assert.equal(view.confirm.disabled, true);
  });
}
