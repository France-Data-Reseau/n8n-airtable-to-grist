import { readFileSync } from 'node:fs';
import vm from 'node:vm';

const request = JSON.parse(readFileSync(0, 'utf8'));
const items = request.items ?? [];
const all = (values) => ({
  all: () => values,
  first: () => {
    if (!values.length) throw new Error('No input item');
    return values[0];
  },
});
const sandbox = {
  $input: all(items),
  $: (name) => {
    if (!(name in request.outputs)) throw new Error(`Node not executed: ${name}`);
    return all(request.outputs[name]);
  },
  $json: items[0]?.json,
  $response: request.response,
};
const script = request.mode === 'expression'
  ? `(${request.source.slice(3, -2)})`
  : `(function () { ${request.source}\n })()`;
const result = vm.runInNewContext(script, sandbox, { timeout: 5000 });
process.stdout.write(JSON.stringify(result) ?? 'null');
