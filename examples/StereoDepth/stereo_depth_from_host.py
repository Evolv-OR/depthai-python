#!/usr/bin/env python3

import cv2
import numpy as np
import depthai as dai
from time import sleep
import datetime
import argparse
from pathlib import Path
import math
import os, re

datasetDefault = str((Path(__file__).parent / Path("../models/dataset")).resolve().absolute())
parser = argparse.ArgumentParser()
parser.add_argument("-p", "--dataset", nargs="?", help="Path to recorded frames", default=None)
parser.add_argument("-d", "--debug", action="store_true", help="Enable debug outputs.")
parser.add_argument("-e", "--evaluate", help="Evaluate the disparity calculation.", default=None)
parser.add_argument("-dumpdispcost", "--dumpdisparitycostvalues", action="store_true", help="Dumps the disparity cost values for each disparity range. 96 byte for each pixel.")
parser.add_argument("--download", action="store_true", help="Downloads the 2014 Middlebury dataset.")
args = parser.parse_args()

if args.evaluate is not None and args.dataset is not None:
    import sys
    raise ValueError("Cannot use both --dataset and --evaluate arguments at the same time.")

evaluation_mode = args.evaluate is not None
args.dataset = args.dataset or datasetDefault

if args.download and args.evaluate is None:
    import sys
    raise ValueError("Cannot use --download without --evaluate argument.")

if args.evaluate is None and not Path(args.dataset).exists():
    import sys
    raise FileNotFoundError(f"Required file/s not found, please run '{sys.executable} install_requirements.py'")

if args.evaluate is not None and not args.download and not Path(args.evaluate).exists():
    import sys
    raise FileNotFoundError(f"Evaluation dataset path does not exist, use the --evaluate argument to specify the path.")

if args.evaluate is not None and args.download and not Path(args.evaluate).exists():
    os.makedirs(args.evaluate)

def download_2014_middlebury(data_path):
    import requests, zipfile, io
    url = "https://vision.middlebury.edu/stereo/data/scenes2014/zip/"
    r = requests.get(url)
    c = r.content
    reg = re.compile(r"href=('|\")(.+\.zip)('|\")")
    matches = reg.findall(c.decode("utf-8"))
    files = [m[1] for m in matches]

    for f in files:
        if os.path.isdir(os.path.join(data_path, f[:-4])):
            print(f"Skipping {f}")
        else:
            print(f"Downloading {f} from {url + f}")
            r = requests.get(url + f)
            print(f"Extracting {f} to {data_path}")
            z = zipfile.ZipFile(io.BytesIO(r.content))
            z.extractall(data_path)

if args.download:
    download_2014_middlebury(args.evaluate)
    if not evaluation_mode:
        sys.exit(0)

class StereoConfigHandler:

    class Trackbar:
        def __init__(self, trackbarName, windowName, minValue, maxValue, defaultValue, handler):
            self.min = minValue
            self.max = maxValue
            self.windowName = windowName
            self.trackbarName = trackbarName
            cv2.createTrackbar(trackbarName, windowName, minValue, maxValue, handler)
            cv2.setTrackbarPos(trackbarName, windowName, defaultValue)

        def set(self, value):
            if value < self.min:
                value = self.min
                print(f"{self.trackbarName} min value is {self.min}")
            if value > self.max:
                value = self.max
                print(f"{self.trackbarName} max value is {self.max}")
            cv2.setTrackbarPos(self.trackbarName, self.windowName, value)

    class CensusMaskHandler:

        stateColor = [(0, 0, 0), (255, 255, 255)]
        gridHeight = 50
        gridWidth = 50

        def fillRectangle(self, row, col):
            src = self.gridList[row][col]["topLeft"]
            dst = self.gridList[row][col]["bottomRight"]

            stateColor = self.stateColor[1] if self.gridList[row][col]["state"] else self.stateColor[0]
            self.changed = True

            cv2.rectangle(self.gridImage, src, dst, stateColor, -1)
            cv2.imshow(self.windowName, self.gridImage)


        def clickCallback(self, event, x, y, flags, param):
            if event == cv2.EVENT_LBUTTONDOWN:
                col = x * (self.gridSize[1]) // self.width
                row = y * (self.gridSize[0]) // self.height
                self.gridList[row][col]["state"] = not self.gridList[row][col]["state"]
                self.fillRectangle(row, col)


        def __init__(self, windowName, gridSize):
            self.gridSize = gridSize
            self.windowName = windowName
            self.changed = False

            cv2.namedWindow(self.windowName)

            self.width = StereoConfigHandler.CensusMaskHandler.gridWidth * self.gridSize[1]
            self.height = StereoConfigHandler.CensusMaskHandler.gridHeight * self.gridSize[0]

            self.gridImage = np.zeros((self.height + 50, self.width, 3), np.uint8)

            cv2.putText(self.gridImage, "Click on grid to change mask!", (0, self.height+20), cv2.FONT_HERSHEY_SIMPLEX, 0.4, (255, 255, 255))
            cv2.putText(self.gridImage, "White: ON   |   Black: OFF", (0, self.height+40), cv2.FONT_HERSHEY_SIMPLEX, 0.4, (255, 255, 255))

            self.gridList = [[dict() for _ in range(self.gridSize[1])] for _ in range(self.gridSize[0])]

            for row in range(self.gridSize[0]):
                rowFactor = self.height // self.gridSize[0]
                srcY = row*rowFactor + 1
                dstY = (row+1)*rowFactor - 1
                for col in range(self.gridSize[1]):
                    colFactor = self.width // self.gridSize[1]
                    srcX = col*colFactor + 1
                    dstX = (col+1)*colFactor - 1
                    src = (srcX, srcY)
                    dst = (dstX, dstY)
                    self.gridList[row][col]["topLeft"] = src
                    self.gridList[row][col]["bottomRight"] = dst
                    self.gridList[row][col]["state"] = False
                    self.fillRectangle(row, col)

            cv2.setMouseCallback(self.windowName, self.clickCallback)
            cv2.imshow(self.windowName, self.gridImage)

        def getMask(self) -> np.uint64:
            mask = np.uint64(0)
            for row in range(self.gridSize[0]):
                for col in range(self.gridSize[1]):
                    if self.gridList[row][col]["state"]:
                        pos = row*self.gridSize[1] + col
                        mask = np.bitwise_or(mask, np.uint64(1) << np.uint64(pos))

            return mask

        def setMask(self, _mask: np.uint64):
            mask = np.uint64(_mask)
            for row in range(self.gridSize[0]):
                for col in range(self.gridSize[1]):
                    pos = row*self.gridSize[1] + col
                    if np.bitwise_and(mask, np.uint64(1) << np.uint64(pos)):
                        self.gridList[row][col]["state"] = True
                    else:
                        self.gridList[row][col]["state"] = False

                    self.fillRectangle(row, col)

        def isChanged(self):
            changed = self.changed
            self.changed = False
            return changed

        def destroyWindow(self):
            cv2.destroyWindow(self.windowName)


    censusMaskHandler = None
    newConfig = False
    config = None
    trSigma = list()
    trConfidence = list()
    trLrCheck = list()
    trFractionalBits = list()
    trLineqAlpha = list()
    trLineqBeta = list()
    trLineqThreshold = list()
    trCostAggregationP1 = list()
    trCostAggregationP2 = list()
    trTemporalAlpha = list()
    trTemporalDelta = list()
    trThresholdMinRange = list()
    trThresholdMaxRange = list()
    trSpeckleRange = list()
    trSpatialAlpha = list()
    trSpatialDelta = list()
    trSpatialHoleFilling = list()
    trSpatialNumIterations = list()
    trDecimationFactor = list()
    trDisparityShift = list()
    trCenterAlignmentShift = list()
    trInvalidateEdgePixels = list()

    def trackbarSigma(value):
        StereoConfigHandler.config.postProcessing.bilateralSigmaValue = value
        StereoConfigHandler.newConfig = True
        for tr in StereoConfigHandler.trSigma:
            tr.set(value)

    def trackbarConfidence(value):
        StereoConfigHandler.config.costMatching.confidenceThreshold = value
        StereoConfigHandler.newConfig = True
        for tr in StereoConfigHandler.trConfidence:
            tr.set(value)

    def trackbarLrCheckThreshold(value):
        StereoConfigHandler.config.algorithmControl.leftRightCheckThreshold = value
        StereoConfigHandler.newConfig = True
        for tr in StereoConfigHandler.trLrCheck:
            tr.set(value)

    def trackbarFractionalBits(value):
        StereoConfigHandler.config.algorithmControl.subpixelFractionalBits = value
        StereoConfigHandler.newConfig = True
        for tr in StereoConfigHandler.trFractionalBits:
            tr.set(value)

    def trackbarLineqAlpha(value):
        StereoConfigHandler.config.costMatching.linearEquationParameters.alpha = value
        StereoConfigHandler.newConfig = True
        for tr in StereoConfigHandler.trLineqAlpha:
            tr.set(value)

    def trackbarLineqBeta(value):
        StereoConfigHandler.config.costMatching.linearEquationParameters.beta = value
        StereoConfigHandler.newConfig = True
        for tr in StereoConfigHandler.trLineqBeta:
            tr.set(value)

    def trackbarLineqThreshold(value):
        StereoConfigHandler.config.costMatching.linearEquationParameters.threshold = value
        StereoConfigHandler.newConfig = True
        for tr in StereoConfigHandler.trLineqThreshold:
            tr.set(value)

    def trackbarCostAggregationP1(value):
        StereoConfigHandler.config.costAggregation.horizontalPenaltyCostP1 = value
        StereoConfigHandler.config.costAggregation.verticalPenaltyCostP1 = value
        StereoConfigHandler.newConfig = True
        for tr in StereoConfigHandler.trCostAggregationP1:
            tr.set(value)

    def trackbarCostAggregationP2(value):
        StereoConfigHandler.config.costAggregation.horizontalPenaltyCostP2 = value
        StereoConfigHandler.config.costAggregation.verticalPenaltyCostP2 = value
        StereoConfigHandler.newConfig = True
        for tr in StereoConfigHandler.trCostAggregationP2:
            tr.set(value)

    def trackbarTemporalFilterAlpha(value):
        StereoConfigHandler.config.postProcessing.temporalFilter.alpha = value / 100.
        StereoConfigHandler.newConfig = True
        for tr in StereoConfigHandler.trTemporalAlpha:
            tr.set(value)

    def trackbarTemporalFilterDelta(value):
        StereoConfigHandler.config.postProcessing.temporalFilter.delta = value
        StereoConfigHandler.newConfig = True
        for tr in StereoConfigHandler.trTemporalDelta:
            tr.set(value)

    def trackbarSpatialFilterAlpha(value):
        StereoConfigHandler.config.postProcessing.spatialFilter.alpha = value / 100.
        StereoConfigHandler.newConfig = True
        for tr in StereoConfigHandler.trSpatialAlpha:
            tr.set(value)

    def trackbarSpatialFilterDelta(value):
        StereoConfigHandler.config.postProcessing.spatialFilter.delta = value
        StereoConfigHandler.newConfig = True
        for tr in StereoConfigHandler.trSpatialDelta:
            tr.set(value)

    def trackbarSpatialFilterHoleFillingRadius(value):
        StereoConfigHandler.config.postProcessing.spatialFilter.holeFillingRadius = value
        StereoConfigHandler.newConfig = True
        for tr in StereoConfigHandler.trSpatialHoleFilling:
            tr.set(value)

    def trackbarSpatialFilterNumIterations(value):
        StereoConfigHandler.config.postProcessing.spatialFilter.numIterations = value
        StereoConfigHandler.newConfig = True
        for tr in StereoConfigHandler.trSpatialNumIterations:
            tr.set(value)

    def trackbarThresholdMinRange(value):
        StereoConfigHandler.config.postProcessing.thresholdFilter.minRange = value * 1000
        StereoConfigHandler.newConfig = True
        for tr in StereoConfigHandler.trThresholdMinRange:
            tr.set(value)

    def trackbarThresholdMaxRange(value):
        StereoConfigHandler.config.postProcessing.thresholdFilter.maxRange = value * 1000
        StereoConfigHandler.newConfig = True
        for tr in StereoConfigHandler.trThresholdMaxRange:
            tr.set(value)

    def trackbarSpeckleRange(value):
        StereoConfigHandler.config.postProcessing.speckleFilter.speckleRange = value
        StereoConfigHandler.newConfig = True
        for tr in StereoConfigHandler.trSpeckleRange:
            tr.set(value)

    def trackbarDecimationFactor(value):
        StereoConfigHandler.config.postProcessing.decimationFilter.decimationFactor = value
        StereoConfigHandler.newConfig = True
        for tr in StereoConfigHandler.trDecimationFactor:
            tr.set(value)

    def trackbarDisparityShift(value):
        StereoConfigHandler.config.algorithmControl.disparityShift = value
        StereoConfigHandler.newConfig = True
        for tr in StereoConfigHandler.trDisparityShift:
            tr.set(value)

    def trackbarCenterAlignmentShift(value):
        if StereoConfigHandler.config.algorithmControl.depthAlign != dai.StereoDepthConfig.AlgorithmControl.DepthAlign.CENTER:
            print("Center alignment shift factor requires CENTER alignment enabled!")
            return
        StereoConfigHandler.config.algorithmControl.centerAlignmentShiftFactor = value / 100.
        print(f"centerAlignmentShiftFactor: {StereoConfigHandler.config.algorithmControl.centerAlignmentShiftFactor:.2f}")
        StereoConfigHandler.newConfig = True
        for tr in StereoConfigHandler.trCenterAlignmentShift:
            tr.set(value)

    def trackbarInvalidateEdgePixels(value):
        StereoConfigHandler.config.algorithmControl.numInvalidateEdgePixels = value
        print(f"numInvalidateEdgePixels: {StereoConfigHandler.config.algorithmControl.numInvalidateEdgePixels:.2f}")
        StereoConfigHandler.newConfig = True
        for tr in StereoConfigHandler.trInvalidateEdgePixels:
            tr.set(value)

    def handleKeypress(key, stereoDepthConfigInQueue):
        if key == ord("m"):
            StereoConfigHandler.newConfig = True
            medianSettings = [dai.MedianFilter.MEDIAN_OFF, dai.MedianFilter.KERNEL_3x3, dai.MedianFilter.KERNEL_5x5, dai.MedianFilter.KERNEL_7x7]
            currentMedian = StereoConfigHandler.config.postProcessing.median
            nextMedian = medianSettings[(medianSettings.index(currentMedian)+1) % len(medianSettings)]
            print(f"Changing median to {nextMedian.name} from {currentMedian.name}")
            StereoConfigHandler.config.postProcessing.median = nextMedian
        if key == ord("w"):
            StereoConfigHandler.newConfig = True
            StereoConfigHandler.config.postProcessing.spatialFilter.enable = not StereoConfigHandler.config.postProcessing.spatialFilter.enable
            state = "on" if StereoConfigHandler.config.postProcessing.spatialFilter.enable else "off"
            print(f"Spatial filter {state}")
        if key == ord("t"):
            StereoConfigHandler.newConfig = True
            StereoConfigHandler.config.postProcessing.temporalFilter.enable = not StereoConfigHandler.config.postProcessing.temporalFilter.enable
            state = "on" if StereoConfigHandler.config.postProcessing.temporalFilter.enable else "off"
            print(f"Temporal filter {state}")
        if key == ord("s"):
            StereoConfigHandler.newConfig = True
            StereoConfigHandler.config.postProcessing.speckleFilter.enable = not StereoConfigHandler.config.postProcessing.speckleFilter.enable
            state = "on" if StereoConfigHandler.config.postProcessing.speckleFilter.enable else "off"
            print(f"Speckle filter {state}")
        if key == ord("r"):
            StereoConfigHandler.newConfig = True
            temporalSettings = [dai.StereoDepthConfig.PostProcessing.TemporalFilter.PersistencyMode.PERSISTENCY_OFF,
            dai.StereoDepthConfig.PostProcessing.TemporalFilter.PersistencyMode.VALID_8_OUT_OF_8,
            dai.StereoDepthConfig.PostProcessing.TemporalFilter.PersistencyMode.VALID_2_IN_LAST_3,
            dai.StereoDepthConfig.PostProcessing.TemporalFilter.PersistencyMode.VALID_2_IN_LAST_4,
            dai.StereoDepthConfig.PostProcessing.TemporalFilter.PersistencyMode.VALID_2_OUT_OF_8,
            dai.StereoDepthConfig.PostProcessing.TemporalFilter.PersistencyMode.VALID_1_IN_LAST_2,
            dai.StereoDepthConfig.PostProcessing.TemporalFilter.PersistencyMode.VALID_1_IN_LAST_5,
            dai.StereoDepthConfig.PostProcessing.TemporalFilter.PersistencyMode.VALID_1_IN_LAST_8,
            dai.StereoDepthConfig.PostProcessing.TemporalFilter.PersistencyMode.PERSISTENCY_INDEFINITELY,
            ]
            currentTemporal = StereoConfigHandler.config.postProcessing.temporalFilter.persistencyMode
            nextTemporal = temporalSettings[(temporalSettings.index(currentTemporal)+1) % len(temporalSettings)]
            print(f"Changing temporal persistency to {nextTemporal.name} from {currentTemporal.name}")
            StereoConfigHandler.config.postProcessing.temporalFilter.persistencyMode = nextTemporal
        if key == ord("n"):
            StereoConfigHandler.newConfig = True
            decimationSettings = [dai.StereoDepthConfig.PostProcessing.DecimationFilter.DecimationMode.PIXEL_SKIPPING,
            dai.StereoDepthConfig.PostProcessing.DecimationFilter.DecimationMode.NON_ZERO_MEDIAN,
            dai.StereoDepthConfig.PostProcessing.DecimationFilter.DecimationMode.NON_ZERO_MEAN,
            ]
            currentDecimation = StereoConfigHandler.config.postProcessing.decimationFilter.decimationMode
            nextDecimation = decimationSettings[(decimationSettings.index(currentDecimation)+1) % len(decimationSettings)]
            print(f"Changing decimation mode to {nextDecimation.name} from {currentDecimation.name}")
            StereoConfigHandler.config.postProcessing.decimationFilter.decimationMode = nextDecimation
        if key == ord("a"):
            StereoConfigHandler.newConfig = True
            alignmentSettings = [dai.StereoDepthConfig.AlgorithmControl.DepthAlign.RECTIFIED_RIGHT,
            dai.StereoDepthConfig.AlgorithmControl.DepthAlign.RECTIFIED_LEFT,
            dai.StereoDepthConfig.AlgorithmControl.DepthAlign.CENTER,
            ]
            currentAlignment = StereoConfigHandler.config.algorithmControl.depthAlign
            nextAlignment = alignmentSettings[(alignmentSettings.index(currentAlignment)+1) % len(alignmentSettings)]
            print(f"Changing alignment mode to {nextAlignment.name} from {currentAlignment.name}")
            StereoConfigHandler.config.algorithmControl.depthAlign = nextAlignment
        elif key == ord("c"):
            StereoConfigHandler.newConfig = True
            censusSettings = [dai.StereoDepthConfig.CensusTransform.KernelSize.AUTO, dai.StereoDepthConfig.CensusTransform.KernelSize.KERNEL_5x5, dai.StereoDepthConfig.CensusTransform.KernelSize.KERNEL_7x7, dai.StereoDepthConfig.CensusTransform.KernelSize.KERNEL_7x9]
            currentCensus = StereoConfigHandler.config.censusTransform.kernelSize
            nextCensus = censusSettings[(censusSettings.index(currentCensus)+1) % len(censusSettings)]
            if nextCensus != dai.StereoDepthConfig.CensusTransform.KernelSize.AUTO:
                censusGridSize = [(5,5), (7,7), (7,9)]
                censusDefaultMask = [np.uint64(0XA82415), np.uint64(0XAA02A8154055), np.uint64(0X2AA00AA805540155)]
                censusGrid = censusGridSize[nextCensus]
                censusMask = censusDefaultMask[nextCensus]
                StereoConfigHandler.censusMaskHandler = StereoConfigHandler.CensusMaskHandler("Census mask", censusGrid)
                StereoConfigHandler.censusMaskHandler.setMask(censusMask)
            else:
                print("Census mask config is not available in AUTO census kernel mode. Change using the 'c' key")
                StereoConfigHandler.config.censusTransform.kernelMask = 0
                StereoConfigHandler.censusMaskHandler.destroyWindow()
            print(f"Changing census transform to {nextCensus.name} from {currentCensus.name}")
            StereoConfigHandler.config.censusTransform.kernelSize = nextCensus
        elif key == ord("d"):
            StereoConfigHandler.newConfig = True
            dispRangeSettings = [dai.StereoDepthConfig.CostMatching.DisparityWidth.DISPARITY_64, dai.StereoDepthConfig.CostMatching.DisparityWidth.DISPARITY_96]
            currentDispRange = StereoConfigHandler.config.costMatching.disparityWidth
            nextDispRange = dispRangeSettings[(dispRangeSettings.index(currentDispRange)+1) % len(dispRangeSettings)]
            print(f"Changing disparity range to {nextDispRange.name} from {currentDispRange.name}")
            StereoConfigHandler.config.costMatching.disparityWidth = nextDispRange
        elif key == ord("f"):
            StereoConfigHandler.newConfig = True
            StereoConfigHandler.config.costMatching.enableCompanding = not StereoConfigHandler.config.costMatching.enableCompanding
            state = "on" if StereoConfigHandler.config.costMatching.enableCompanding else "off"
            print(f"Companding {state}")
        elif key == ord("v"):
            StereoConfigHandler.newConfig = True
            StereoConfigHandler.config.censusTransform.enableMeanMode = not StereoConfigHandler.config.censusTransform.enableMeanMode
            state = "on" if StereoConfigHandler.config.censusTransform.enableMeanMode else "off"
            print(f"Census transform mean mode {state}")
        elif key == ord("1"):
            StereoConfigHandler.newConfig = True
            StereoConfigHandler.config.algorithmControl.enableLeftRightCheck = not StereoConfigHandler.config.algorithmControl.enableLeftRightCheck
            state = "on" if StereoConfigHandler.config.algorithmControl.enableLeftRightCheck else "off"
            print(f"LR-check {state}")
        elif key == ord("2"):
            StereoConfigHandler.newConfig = True
            StereoConfigHandler.config.algorithmControl.enableSubpixel = not StereoConfigHandler.config.algorithmControl.enableSubpixel
            state = "on" if StereoConfigHandler.config.algorithmControl.enableSubpixel else "off"
            print(f"Subpixel {state}")
        elif key == ord("3"):
            StereoConfigHandler.newConfig = True
            StereoConfigHandler.config.algorithmControl.enableExtended = not StereoConfigHandler.config.algorithmControl.enableExtended
            state = "on" if StereoConfigHandler.config.algorithmControl.enableExtended else "off"
            print(f"Extended {state}")

        censusMaskChanged = False
        if StereoConfigHandler.censusMaskHandler is not None:
            censusMaskChanged = StereoConfigHandler.censusMaskHandler.isChanged()
        if censusMaskChanged:
            StereoConfigHandler.config.censusTransform.kernelMask = StereoConfigHandler.censusMaskHandler.getMask()
            StereoConfigHandler.newConfig = True

        StereoConfigHandler.sendConfig(stereoDepthConfigInQueue)

    def sendConfig(stereoDepthConfigInQueue):
        if StereoConfigHandler.newConfig:
            StereoConfigHandler.newConfig = False
            configMessage = dai.StereoDepthConfig()
            configMessage.set(StereoConfigHandler.config)
            stereoDepthConfigInQueue.send(configMessage)

    def updateDefaultConfig(config):
        StereoConfigHandler.config = config

    def registerWindow(stream):
        cv2.namedWindow(stream, cv2.WINDOW_NORMAL)

        StereoConfigHandler.trConfidence.append(StereoConfigHandler.Trackbar("Disparity confidence", stream, 0, 255, StereoConfigHandler.config.costMatching.confidenceThreshold, StereoConfigHandler.trackbarConfidence))
        StereoConfigHandler.trSigma.append(StereoConfigHandler.Trackbar("Bilateral sigma", stream, 0, 100, StereoConfigHandler.config.postProcessing.bilateralSigmaValue, StereoConfigHandler.trackbarSigma))
        StereoConfigHandler.trLrCheck.append(StereoConfigHandler.Trackbar("LR-check threshold", stream, 0, 16, StereoConfigHandler.config.algorithmControl.leftRightCheckThreshold, StereoConfigHandler.trackbarLrCheckThreshold))
        StereoConfigHandler.trFractionalBits.append(StereoConfigHandler.Trackbar("Subpixel fractional bits", stream, 3, 5, StereoConfigHandler.config.algorithmControl.subpixelFractionalBits, StereoConfigHandler.trackbarFractionalBits))
        StereoConfigHandler.trDisparityShift.append(StereoConfigHandler.Trackbar("Disparity shift", stream, 0, 100, StereoConfigHandler.config.algorithmControl.disparityShift, StereoConfigHandler.trackbarDisparityShift))
        StereoConfigHandler.trCenterAlignmentShift.append(StereoConfigHandler.Trackbar("Center alignment shift factor", stream, 0, 100, StereoConfigHandler.config.algorithmControl.centerAlignmentShiftFactor, StereoConfigHandler.trackbarCenterAlignmentShift))
        StereoConfigHandler.trInvalidateEdgePixels.append(StereoConfigHandler.Trackbar("Invalidate edge pixels", stream, 0, 100, StereoConfigHandler.config.algorithmControl.numInvalidateEdgePixels, StereoConfigHandler.trackbarInvalidateEdgePixels))
        StereoConfigHandler.trLineqAlpha.append(StereoConfigHandler.Trackbar("Linear equation alpha", stream, 0, 15, StereoConfigHandler.config.costMatching.linearEquationParameters.alpha, StereoConfigHandler.trackbarLineqAlpha))
        StereoConfigHandler.trLineqBeta.append(StereoConfigHandler.Trackbar("Linear equation beta", stream, 0, 15, StereoConfigHandler.config.costMatching.linearEquationParameters.beta, StereoConfigHandler.trackbarLineqBeta))
        StereoConfigHandler.trLineqThreshold.append(StereoConfigHandler.Trackbar("Linear equation threshold", stream, 0, 255, StereoConfigHandler.config.costMatching.linearEquationParameters.threshold, StereoConfigHandler.trackbarLineqThreshold))
        StereoConfigHandler.trCostAggregationP1.append(StereoConfigHandler.Trackbar("Cost aggregation P1", stream, 0, 500, StereoConfigHandler.config.costAggregation.horizontalPenaltyCostP1, StereoConfigHandler.trackbarCostAggregationP1))
        StereoConfigHandler.trCostAggregationP2.append(StereoConfigHandler.Trackbar("Cost aggregation P2", stream, 0, 500, StereoConfigHandler.config.costAggregation.horizontalPenaltyCostP2, StereoConfigHandler.trackbarCostAggregationP2))
        StereoConfigHandler.trTemporalAlpha.append(StereoConfigHandler.Trackbar("Temporal filter alpha", stream, 0, 100, int(StereoConfigHandler.config.postProcessing.temporalFilter.alpha*100), StereoConfigHandler.trackbarTemporalFilterAlpha))
        StereoConfigHandler.trTemporalDelta.append(StereoConfigHandler.Trackbar("Temporal filter delta", stream, 0, 100, StereoConfigHandler.config.postProcessing.temporalFilter.delta, StereoConfigHandler.trackbarTemporalFilterDelta))
        StereoConfigHandler.trSpatialAlpha.append(StereoConfigHandler.Trackbar("Spatial filter alpha", stream, 0, 100, int(StereoConfigHandler.config.postProcessing.spatialFilter.alpha*100), StereoConfigHandler.trackbarSpatialFilterAlpha))
        StereoConfigHandler.trSpatialDelta.append(StereoConfigHandler.Trackbar("Spatial filter delta", stream, 0, 100, StereoConfigHandler.config.postProcessing.spatialFilter.delta, StereoConfigHandler.trackbarSpatialFilterDelta))
        StereoConfigHandler.trSpatialHoleFilling.append(StereoConfigHandler.Trackbar("Spatial filter hole filling radius", stream, 0, 16, StereoConfigHandler.config.postProcessing.spatialFilter.holeFillingRadius, StereoConfigHandler.trackbarSpatialFilterHoleFillingRadius))
        StereoConfigHandler.trSpatialNumIterations.append(StereoConfigHandler.Trackbar("Spatial filter number of iterations", stream, 0, 4, StereoConfigHandler.config.postProcessing.spatialFilter.numIterations, StereoConfigHandler.trackbarSpatialFilterNumIterations))
        StereoConfigHandler.trThresholdMinRange.append(StereoConfigHandler.Trackbar("Threshold filter min range", stream, 0, 65, StereoConfigHandler.config.postProcessing.thresholdFilter.minRange, StereoConfigHandler.trackbarThresholdMinRange))
        StereoConfigHandler.trThresholdMaxRange.append(StereoConfigHandler.Trackbar("Threshold filter max range", stream, 0, 65, StereoConfigHandler.config.postProcessing.thresholdFilter.maxRange, StereoConfigHandler.trackbarThresholdMaxRange))
        StereoConfigHandler.trSpeckleRange.append(StereoConfigHandler.Trackbar("Speckle filter range", stream, 0, 240, StereoConfigHandler.config.postProcessing.speckleFilter.speckleRange, StereoConfigHandler.trackbarSpeckleRange))
        StereoConfigHandler.trDecimationFactor.append(StereoConfigHandler.Trackbar("Decimation factor", stream, 1, 4, StereoConfigHandler.config.postProcessing.decimationFilter.decimationFactor, StereoConfigHandler.trackbarDecimationFactor))

    def __init__(self, config):
        print("Control median filter using the 'm' key.")
        print("Control census transform kernel size using the 'c' key.")
        print("Control disparity search range using the 'd' key.")
        print("Control disparity companding using the 'f' key.")
        print("Control census transform mean mode using the 'v' key.")
        print("Control depth alignment using the 'a' key.")
        print("Control decimation algorithm using the 'a' key.")
        print("Control temporal persistency mode using the 'r' key.")
        print("Control spatial filter using the 'w' key.")
        print("Control temporal filter using the 't' key.")
        print("Control speckle filter using the 's' key.")
        print("Control left-right check mode using the '1' key.")
        print("Control subpixel mode using the '2' key.")
        print("Control extended mode using the '3' key.")
        if evaluation_mode:
            print("Switch between images using '[' and ']' keys.")

        StereoConfigHandler.config = config

        if StereoConfigHandler.config.censusTransform.kernelSize != dai.StereoDepthConfig.CensusTransform.KernelSize.AUTO:
            censusMask = StereoConfigHandler.config.censusTransform.kernelMask
            censusGridSize = [(5,5), (7,7), (7,9)]
            censusGrid = censusGridSize[StereoConfigHandler.config.censusTransform.kernelSize]
            if StereoConfigHandler.config.censusTransform.kernelMask == 0:
                censusDefaultMask = [np.uint64(0xA82415), np.uint64(0xAA02A8154055), np.uint64(0x2AA00AA805540155)]
                censusMask = censusDefaultMask[StereoConfigHandler.config.censusTransform.kernelSize]
            StereoConfigHandler.censusMaskHandler = StereoConfigHandler.CensusMaskHandler("Census mask", censusGrid)
            StereoConfigHandler.censusMaskHandler.setMask(censusMask)
        else:
            print("Census mask config is not available in AUTO Census kernel mode. Change using the 'c' key")


# StereoDepth initial config options.
outDepth = True  # Disparity by default
outConfidenceMap = True  # Output disparity confidence map
outRectified = True   # Output and display rectified streams
lrcheck = True   # Better handling for occlusions
extended = False  # Closer-in minimum depth, disparity range is doubled. Unsupported for now.
subpixel = True   # Better accuracy for longer distance, fractional disparity 32-levels

width = 1280
height = 800

xoutStereoCfg = None

# Create pipeline
pipeline = dai.Pipeline()

# Define sources and outputs
stereo = pipeline.create(dai.node.StereoDepth)

monoLeft = pipeline.create(dai.node.XLinkIn)
monoRight = pipeline.create(dai.node.XLinkIn)
xinStereoDepthConfig = pipeline.create(dai.node.XLinkIn)

xoutLeft = pipeline.create(dai.node.XLinkOut)
xoutRight = pipeline.create(dai.node.XLinkOut)
xoutDepth = pipeline.create(dai.node.XLinkOut)
xoutConfMap = pipeline.create(dai.node.XLinkOut)
xoutDisparity = pipeline.create(dai.node.XLinkOut)
xoutRectifLeft = pipeline.create(dai.node.XLinkOut)
xoutRectifRight = pipeline.create(dai.node.XLinkOut)
xoutStereoCfg = pipeline.create(dai.node.XLinkOut)
if args.debug:
    xoutDebugLrCheckIt1 = pipeline.create(dai.node.XLinkOut)
    xoutDebugLrCheckIt2 = pipeline.create(dai.node.XLinkOut)
    xoutDebugExtLrCheckIt1 = pipeline.create(dai.node.XLinkOut)
    xoutDebugExtLrCheckIt2 = pipeline.create(dai.node.XLinkOut)
if args.dumpdisparitycostvalues:
    xoutDebugCostDump = pipeline.create(dai.node.XLinkOut)

xinStereoDepthConfig.setStreamName("stereoDepthConfig")
monoLeft.setStreamName("in_left")
monoRight.setStreamName("in_right")

xoutLeft.setStreamName("left")
xoutRight.setStreamName("right")
xoutDepth.setStreamName("depth")
xoutConfMap.setStreamName("confidence_map")
xoutDisparity.setStreamName("disparity")
xoutRectifLeft.setStreamName("rectified_left")
xoutRectifRight.setStreamName("rectified_right")
xoutStereoCfg.setStreamName("stereo_cfg")
if args.debug:
    xoutDebugLrCheckIt1.setStreamName("disparity_lr_check_iteration1")
    xoutDebugLrCheckIt2.setStreamName("disparity_lr_check_iteration2")
    xoutDebugExtLrCheckIt1.setStreamName("disparity_ext_lr_check_iteration1")
    xoutDebugExtLrCheckIt2.setStreamName("disparity_ext_lr_check_iteration2")
if args.dumpdisparitycostvalues:
    xoutDebugCostDump.setStreamName("disparity_cost_dump")

# Properties
stereo.setDefaultProfilePreset(dai.node.StereoDepth.PresetMode.HIGH_DENSITY)
stereo.setRectifyEdgeFillColor(0) # Black, to better see the cutout
stereo.setLeftRightCheck(lrcheck)
stereo.setExtendedDisparity(extended)
stereo.setSubpixel(subpixel)

# Switching depthAlign mode at runtime is not supported while aligning to a specific camera is enabled
# stereo.setDepthAlign(dai.CameraBoardSocket.LEFT)

# allocates resources for worst case scenario
# allowing runtime switch of stereo modes
stereo.setRuntimeModeSwitch(True)

# Linking
monoLeft.out.link(stereo.left)
monoRight.out.link(stereo.right)
xinStereoDepthConfig.out.link(stereo.inputConfig)
stereo.syncedLeft.link(xoutLeft.input)
stereo.syncedRight.link(xoutRight.input)
if outDepth:
    stereo.depth.link(xoutDepth.input)
if outConfidenceMap:
    stereo.confidenceMap.link(xoutConfMap.input)
stereo.disparity.link(xoutDisparity.input)
if outRectified:
    stereo.rectifiedLeft.link(xoutRectifLeft.input)
    stereo.rectifiedRight.link(xoutRectifRight.input)
stereo.outConfig.link(xoutStereoCfg.input)
if args.debug:
    stereo.debugDispLrCheckIt1.link(xoutDebugLrCheckIt1.input)
    stereo.debugDispLrCheckIt2.link(xoutDebugLrCheckIt2.input)
    stereo.debugExtDispLrCheckIt1.link(xoutDebugExtLrCheckIt1.input)
    stereo.debugExtDispLrCheckIt2.link(xoutDebugExtLrCheckIt2.input)
if args.dumpdisparitycostvalues:
    stereo.debugDispCostDump.link(xoutDebugCostDump.input)


StereoConfigHandler(stereo.initialConfig.get())
StereoConfigHandler.registerWindow("Stereo control panel")

# stereo.setPostProcessingHardwareResources(3, 3)

stereo.setInputResolution(width, height)
stereo.setRectification(False)
baseline = 75
fov = 71.86
focal = width / (2 * math.tan(fov / 2 / 180 * math.pi))

stereo.setBaseline(baseline/10)
stereo.setFocalLength(focal)

streams = ['left', 'right']
if outRectified:
    streams.extend(["rectified_left", "rectified_right"])
streams.append("disparity")
if outDepth:
    streams.append("depth")
if outConfidenceMap:
    streams.append("confidence_map")
debugStreams = []
if args.debug:
    debugStreams.extend(["disparity_lr_check_iteration1", "disparity_lr_check_iteration2"])
    debugStreams.extend(["disparity_ext_lr_check_iteration1", "disparity_ext_lr_check_iteration2"])
if args.dumpdisparitycostvalues:
    debugStreams.append("disparity_cost_dump")

def convertToCv2Frame(name, image, config):

    maxDisp = config.getMaxDisparity()
    subpixelLevels = pow(2, config.get().algorithmControl.subpixelFractionalBits)
    subpixel = config.get().algorithmControl.enableSubpixel
    dispIntegerLevels = maxDisp if not subpixel else maxDisp / subpixelLevels

    frame = image.getFrame()

    # frame.tofile(name+".raw")

    if name == "depth":
        dispScaleFactor = baseline * focal
        with np.errstate(divide="ignore"):
            frame = dispScaleFactor / frame

        frame = (frame * 255. / dispIntegerLevels).astype(np.uint8)
        frame = cv2.applyColorMap(frame, cv2.COLORMAP_HOT)
    elif "confidence_map" in name:
        pass
    elif name == "disparity_cost_dump":
        # frame.tofile(name+".raw")
        pass
    elif "disparity" in name:
        if 1: # Optionally, extend disparity range to better visualize it
            frame = (frame * 255. / maxDisp).astype(np.uint8)
        return frame
        # if 1: # Optionally, apply a color map
        #     frame = cv2.applyColorMap(frame, cv2.COLORMAP_HOT)

    return frame

class DatasetManager:
    def __init__(self, path):
        self.path = path
        self.index = 0
        self.names = [d for d in os.listdir(path) if os.path.isdir(os.path.join(path, d))]
        if len(self.names) == 0:
            raise RuntimeError("No dataset found at {}".format(path))

    def get(self):
        return os.path.join(self.path, self.names[self.index])

    def get_name(self):
        return self.names[self.index]

    def next(self):
        self.index = (self.index + 1) % len(self.names)
        return self.get()

    def prev(self):
        self.index = (self.index - 1) % len(self.names)
        return self.get()


def read_pfm(file):
    file = open(file, "rb")

    color = None
    width = None
    height = None
    scale = None
    endian = None

    header = file.readline().rstrip()
    if header.decode("ascii") == "PF":
        color = True
    elif header.decode("ascii") == "Pf":
        color = False
    else:
        raise Exception("Not a PFM file.")

    dim_match = re.search(r"(\d+)\s(\d+)", file.readline().decode("ascii"))
    if dim_match:
        width, height = map(int, dim_match.groups())
    else:
        raise Exception("Malformed PFM header.")

    scale = float(file.readline().rstrip())
    if scale < 0: # little-endian
        endian = "<"
        scale = -scale
    else:
        endian = ">" # big-endian

    data = np.fromfile(file, endian + "f")
    shape = (height, width, 3) if color else (height, width)
    return np.flip(np.reshape(data, shape), axis=0), scale

def calculate_err_measures(gt_img, oak_img):
    assert gt_img.shape == oak_img.shape

    gt_mask = gt_img != np.inf
    oak_mask = oak_img != np.inf
    mask = gt_mask & oak_mask

    gt_img[~gt_mask] = 0.
    oak_img[~mask] = 0.
    err = np.abs(gt_img - oak_img)

    n = np.sum(gt_mask)
    invalid = np.sum(gt_mask & ~oak_mask)

    bad05 = np.sum(mask & (err > 0.5))
    bad1 = np.sum(mask & (err > 1.))
    bad2 = np.sum(mask & (err > 2.))
    bad4 = np.sum(mask & (err > 4.))
    sum_err = np.sum(err[mask])
    sum_sq_err = np.sum(err[mask] ** 2)
    errs = err[mask]

    bad05_p = 100. * bad05 / n
    total_bad05_p = 100. * (bad05 + invalid) / n
    bad1_p = 100. * bad1 / n
    total_bad1_p = 100. * (bad1 + invalid) / n
    bad2_p = 100. * bad2 / n
    total_bad2_p = 100. * (bad2 + invalid) / n
    bad4_p = 100. * bad4 / n
    total_bad4_p = 100. * (bad4 + invalid) / n
    invalid_p = 100. * invalid / n
    avg_err = sum_err / (n - invalid)
    mse = sum_sq_err / (n - invalid)
    a50 = np.percentile(errs, 50)
    a90 = np.percentile(errs, 90)
    a95 = np.percentile(errs, 95)
    a99 = np.percentile(errs, 99)

    return {
        "bad0.5": bad05_p,
        "total_bad0.5": total_bad05_p,
        "bad1": bad1_p,
        "total_bad1": total_bad1_p,
        "bad2": bad2_p,
        "total_bad2": total_bad2_p,
        "bad4": bad4_p,
        "total_bad4": total_bad4_p,
        "invalid": invalid_p,
        "avg_err": avg_err,
        "mse": mse,
        "a50": a50,
        "a90": a90,
        "a95": a95,
        "a99": a99
    }

def show_evaluation(img_name, evals):
    cv2.namedWindow("Evaluation", cv2.WINDOW_NORMAL)
    font = cv2.FONT_HERSHEY_SIMPLEX
    font_scale = 2
    thickness = 3
    color = (0, 0, 0)
    lines = [
        f"Name: {img_name}",
        f"Bad0.5: {evals['bad0.5']:.2f}%",
        f"Total Bad0.5: {evals['total_bad0.5']:.2f}%",
        f"Bad1: {evals['bad1']:.2f}%",
        f"Total Bad1: {evals['total_bad1']:.2f}%",
        f"Bad2: {evals['bad2']:.2f}%",
        f"Total Bad2: {evals['total_bad2']:.2f}%",
        f"Bad4: {evals['bad4']:.2f}%",
        f"Total Bad4: {evals['total_bad4']:.2f}%",
        f"Invalid: {evals['invalid']:.2f}%",
        f"Avg Err: {evals['avg_err']:.2f}",
        f"MSE: {evals['mse']:.2f}",
        f"A50: {evals['a50']:.2f}",
        f"A90: {evals['a90']:.2f}",
        f"A95: {evals['a95']:.2f}",
        f"A99: {evals['a99']:.2f}"
    ]
    sizes = [cv2.getTextSize(line, font, font_scale, thickness) for line in lines]
    sizes = [(size[0][0], size[0][1] + size[1], size[1]) for size in sizes]
    max_width = max([size[0] for size in sizes])
    total_height = sum([size[1] for size in sizes]) + (len(lines) - 1) * thickness
    img = np.ones((total_height + thickness, max_width, 3), dtype=np.uint8) * 255
    y = 0
    for line, size in zip(lines, sizes):
        cv2.putText(img, line, (0, y + size[1] - size[2]), font, font_scale, color, thickness)
        y += size[1] + thickness
    cv2.imshow("Evaluation", img)

def show_debug_disparity(gt_img, oak_img):
    def rescale_img(img):
        img[img == np.inf] = 0.
        img = cv2.resize(img, (1280, 800), interpolation=cv2.INTER_AREA)
        return img.astype(np.uint16)

    gt_img = rescale_img(gt_img)
    oak_img = rescale_img(oak_img)
    maxv = max(gt_img.max(), oak_img.max())
    gt_img = (gt_img * 255. / maxv).astype(np.uint8)
    oak_img = (oak_img * 255. / maxv).astype(np.uint8)
    cv2.imshow("GT", gt_img)
    cv2.imshow("OAK", oak_img)

if evaluation_mode:
    dataset = DatasetManager(args.evaluate)

print("Connecting and starting the pipeline")
# Connect to device and start pipeline
with dai.Device(pipeline) as device:

    stereoDepthConfigInQueue = device.getInputQueue("stereoDepthConfig")
    inStreams = ["in_left", "in_right"]
    inStreamsCameraID = [dai.CameraBoardSocket.CAM_B, dai.CameraBoardSocket.CAM_C]
    in_q_list = []
    for s in inStreams:
        q = device.getInputQueue(s)
        in_q_list.append(q)

    # Create a receive queue for each stream
    q_list = []
    for s in streams:
        q = device.getOutputQueue(s, 8, blocking=False)
        q_list.append(q)

    inCfg = device.getOutputQueue("stereo_cfg", 8, blocking=False)

    # Need to set a timestamp for input frames, for the sync stage in Stereo node
    timestamp_ms = 0
    index = 0
    prevQueues = q_list.copy()
    while True:
        # Handle input streams, if any
        if in_q_list:
            dataset_size = 1  # Number of image pairs
            frame_interval_ms = 50
            for i, q in enumerate(in_q_list):
                path = os.path.join(dataset.get(), f"im{i}.png") if evaluation_mode else args.dataset + "/" + str(index) + "/" + q.getName() + ".png"
                data = cv2.imread(path, cv2.IMREAD_GRAYSCALE)
                data = cv2.resize(data, (width, height), interpolation = cv2.INTER_AREA)
                data = data.reshape(height*width)
                tstamp = datetime.timedelta(seconds = timestamp_ms // 1000,
                                            milliseconds = timestamp_ms % 1000)
                img = dai.ImgFrame()
                img.setData(data)
                img.setTimestamp(tstamp)
                img.setInstanceNum(inStreamsCameraID[i])
                img.setType(dai.ImgFrame.Type.RAW8)
                img.setWidth(width)
                img.setHeight(height)
                q.send(img)
                # print("Sent frame: {:25s}".format(path), "timestamp_ms:", timestamp_ms)
            timestamp_ms += frame_interval_ms
            index = (index + 1) % dataset_size
            sleep(frame_interval_ms / 1000)

        gt_disparity = None
        if evaluation_mode:
            # Load GT disparity
            gt_disparity = read_pfm(os.path.join(dataset.get(), f"disp1.pfm"))[0]

        # Handle output streams
        currentConfig = inCfg.get()

        lrCheckEnabled = currentConfig.get().algorithmControl.enableLeftRightCheck
        extendedEnabled = currentConfig.get().algorithmControl.enableExtended
        queues = q_list.copy()

        if args.dumpdisparitycostvalues:
            q = device.getOutputQueue("disparity_cost_dump", 8, blocking=False)
            queues.append(q)

        if args.debug:
            q_list_debug = []

            activeDebugStreams = []
            if lrCheckEnabled:
                activeDebugStreams.extend(["disparity_lr_check_iteration1", "disparity_lr_check_iteration2"])
            if extendedEnabled:
                activeDebugStreams.extend(["disparity_ext_lr_check_iteration1"])
                if lrCheckEnabled:
                    activeDebugStreams.extend(["disparity_ext_lr_check_iteration2"])

            for s in activeDebugStreams:
                q = device.getOutputQueue(s, 8, blocking=False)
                q_list_debug.append(q)

            queues.extend(q_list_debug)

        def ListDiff(li1, li2):
            return list(set(li1) - set(li2)) + list(set(li2) - set(li1))

        diff = ListDiff(prevQueues, queues)
        for s in diff:
            name = s.getName()
            cv2.destroyWindow(name)
        prevQueues = queues.copy()

        disparity = None
        for q in queues:
            if q.getName() in ["left", "right"]: continue
            data = q.get()
            if q.getName() == "disparity":
                disparity = data.getFrame()
            frame = convertToCv2Frame(q.getName(), data, currentConfig)
            cv2.imshow(q.getName(), frame)

        if disparity is not None and gt_disparity is not None:
            subpixel_bits = 1 << currentConfig.get().algorithmControl.subpixelFractionalBits
            subpixel_enabled = currentConfig.get().algorithmControl.enableSubpixel
            width_scale = float(gt_disparity.shape[1]) / float(disparity.shape[1])

            disparity = disparity.astype(np.float32)
            if subpixel_enabled:
                disparity = disparity / subpixel_bits
            disparity = disparity * width_scale
            disparity = cv2.resize(disparity, (gt_disparity.shape[1], gt_disparity.shape[0]), interpolation = cv2.INTER_LINEAR)
            disparity[disparity == 0.] = np.inf

            # show_debug_disparity(gt_disparity, disparity)
            err_vals = calculate_err_measures(gt_disparity, disparity)
            show_evaluation(dataset.get_name(), err_vals)

        key = cv2.waitKey(1)
        if key == ord("q"):
            break
        elif evaluation_mode and key == ord("["):
            dataset.next()
        elif evaluation_mode and key == ord("]"):
            dataset.prev()

        StereoConfigHandler.handleKeypress(key, stereoDepthConfigInQueue)
