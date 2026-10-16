#!/usr/bin/env python3
"""Test the actual pure graph routines from DamPartition against slow oracles.

Extracting these small routines lets the test use Int IDs without linking the
entire BSC executable and its SAT/Tcl dependencies. Production remains Haskell.
"""
import argparse,subprocess
from pathlib import Path
ROOT=Path(__file__).resolve().parent
ap=argparse.ArgumentParser();ap.add_argument('--ghc',default='ghc');args=ap.parse_args()
s=(ROOT.parent/'src/comp/DamPartition.hs').read_text()
components=s[s.index('components ::'):s.index('data Fifo')]
a=s.index('      let resourceUsers =');b=s.index('          conflicts =',a)
shared='sharedEdges uses =\n'+s[a:b]+'      in shared\n'
p=ROOT/'build/graph-check';p.mkdir(exist_ok=True)
source='''import qualified Data.Map as M
import qualified Data.Set as S
import Data.List (nub,sort,subsequences)
import Control.Monad (forM_,unless)
type AId = Int
star [] = []
star (x:xs) = [(x,y) | y <- xs]
resource = id :: Int -> Int
'''+components+'\n'+shared+'''
slow [] _ = []
slow (x:xs) es = let
    grow s = let t = nub (s ++ [b | (a,b) <- es, a `elem` s] ++ [a | (a,b) <- es, b `elem` s])
             in if length s == length t then sort t else grow t
    g = grow [x]
    in g : slow (filter (`notElem` g) xs) es
check ids es = unless (components ids es == slow (sort ids) es) (error (show (ids,es)))
main = do
  let ids = [0..5]
      graphs = subsequences [(a,b) | a <- ids, b <- ids, a < b]
  forM_ graphs $ \\es -> check ids es
  forM_ graphs $ \\es -> check ids ((-1,0):(5,8):(8,8):es++es)
  let assignments = sequence (replicate 4 (subsequences [0..2]))
  forM_ assignments $ \\accesses -> do
    let uses = zip [0..] accesses
        clique = [(a,b) | (a,au) <- uses, (b,bu) <- uses, a < b, any (`elem` bu) au]
    unless (components [0..3] (sharedEdges uses) == slow [0..3] clique) (error (show uses))
  putStrLn "PASS: 65,536 graph cases and 4,096 resource-sharing cases; same ordered components"
'''
(p/'Main.hs').write_text(source)
subprocess.run([args.ghc,'-O2','-package','containers','-odir',str(p),'-hidir',str(p),str(p/'Main.hs'),'-o',str(p/'test')],check=True)
subprocess.run([str(p/'test')],check=True)
