import { createRequire } from 'module';

const require = createRequire(import.meta.url);
const { classifyImage } = require('./wrapper.js');

if (process.argv.length !== 3) {
  throw new Error('Incorrect arguments: node classify_new.mjs <IMAGE_FILE>');
}

const predictions = await classifyImage(process.argv[2]);
console.log('Classification Results:', predictions);
