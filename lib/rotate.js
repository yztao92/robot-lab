/** Clockwise display rotation of a width×height image: the canvas size and its setTransform arguments. */
export function rotation(deg, width, height) {
  switch (deg) {
    case 90: return {width: height, height: width, matrix: [0, 1, -1, 0, height, 0]};
    case 180: return {width, height, matrix: [-1, 0, 0, -1, width, height]};
    case 270: return {width: height, height: width, matrix: [0, -1, 1, 0, 0, width]};
    default: return {width, height, matrix: [1, 0, 0, 1, 0, 0]};
  }
}
