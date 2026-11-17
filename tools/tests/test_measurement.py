"""Checks for benchmark reporting errors that could misstate the evidence."""

from pathlib import Path
import sys
import unittest

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from catalogue_load import distribution, percentile
from _verification import ROOT, external_output


class MeasurementTests(unittest.TestCase):
    def test_nearest_rank_uses_all_samples_and_includes_tail(self):
        samples=list(range(100,0,-1))
        self.assertEqual(percentile(samples,.95),95)
        self.assertEqual(percentile(samples,.99),99)
        self.assertEqual(percentile(samples,1),100)
        self.assertEqual(percentile([3],.95),3)
        self.assertEqual(samples[0],100)

    def test_missing_samples_are_unknown_and_nonfinite_samples_are_rejected(self):
        result=distribution([])
        self.assertEqual(result['samples'],0)
        self.assertIsNone(result['p95'])
        self.assertIsNone(result['max'])
        for value in [float('nan'),float('inf'),float('-inf')]:
            with self.assertRaises(ValueError): distribution([.1,value])
        for quantile in [0,-.1,1.1]:
            with self.assertRaises(ValueError): percentile([1],quantile)

    def test_generated_results_cannot_be_written_into_repository(self):
        with self.assertRaises(ValueError): external_output(ROOT/'benchmark-result.json')
        with self.assertRaises(ValueError): external_output(ROOT/'docs'/'benchmark-result.json')


if __name__=='__main__':
    unittest.main()
