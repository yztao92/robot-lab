import {test} from 'node:test';
import assert from 'node:assert/strict';
import {HeadingController} from '../lib/heading.js';
test('DOA crosses zero by the short path without wrapping expression joints',()=>{
  const heading=new HeadingController(60);heading.current=359;heading.setTarget(1);
  heading.tick(1/60);assert.equal(heading.current,0);heading.tick(1/60);assert.equal(heading.current,1);
  heading.setTarget(359);heading.tick(1);assert.equal(heading.current,359);
});
test('DOA stop retains actual simulated heading, rejects non-world angle values',()=>{
  const heading=new HeadingController();heading.setTarget(120);heading.tick(.5);assert.equal(heading.current,30);
  heading.stop();heading.tick(100);assert.equal(heading.current,30);
  for(const invalid of [-1,360,Infinity,NaN])assert.throws(()=>heading.setTarget(invalid));
});
