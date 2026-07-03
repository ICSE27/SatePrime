const tf = require('@tensorflow/tfjs');
const mobilenet = require('@tensorflow-models/mobilenet');
require('@tensorflow/tfjs-node');
const fs = require('fs');

function readImage(path) {
  const imageBuffer = fs.readFileSync(path);
  const tfnode = require('@tensorflow/tfjs-node');
  return tfnode.node.decodeImage(imageBuffer);
}

async function classifyImage(path) {
  const image = readImage(path);
  const model = await mobilenet.load();
  const predictions = await model.classify(image);
  tf.dispose(image);
  return predictions;
}

module.exports = { classifyImage };
