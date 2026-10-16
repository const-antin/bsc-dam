-- Static work estimates, not timing predictions. No application profiling or
-- rule-activation probabilities: every rule and conditional branch is counted.
module DamCost (groupCost, costJSON, costScore, modelJSON, bridgeJSON) where

import ASyntax
import SimPackage (DefMap)
import Prim
import qualified Data.Map as M
import qualified Data.Set as S
import Data.List (foldl', intercalate)

type Cost = M.Map String Integer

one :: String -> Integer -> Cost
one = M.singleton

plus :: Cost -> Cost -> Cost
plus = M.unionWith (+)

weights :: [(String, Integer)]
weights = [("scalar_ops",1), ("word_ops",2), ("mul_word_pairs",4),
           ("div_word_steps",8), ("copy_words",1), ("method_calls",4),
           ("schedule_checks",2), ("unknown_ops",32), ("effects",1000)]

bits :: AType -> Integer
bits (ATBit n) = n
bits (ATArray n t) = n * bits t
bits (ATTuple ts) = sum (map bits ts)
bits _ = 64

words32 :: Integer -> Integer
words32 n = (n + 31) `div` 32

-- Number of limb products retained by wop_mul's truncated output. This
-- closed form avoids iterating over large widths during compilation.
mulPairs :: Integer -> Integer -> Integer -> Integer
mulPairs a b c = triangle c - triangle (c-a) - triangle (c-b) + triangle (c-a-b)
  where triangle n | n <= 0 = 0
                   | otherwise = n * (n+1) `div` 2

opCost :: AType -> PrimOp -> [AExpr] -> Cost
opCost ty op xs
  | out == 0 = M.empty
  | op == PrimMul && width > 64 =
      one "mul_word_pairs" (case map (words32 . bits . ae_type) xs of
                              a:b:_ -> mulPairs a b (words32 out)
                              _ -> words32 width ^ (2 :: Int))
      `plus` one "copy_words" (words32 out)
  | op == PrimMul = one "scalar_ops" 3
  | op `elem` [PrimQuot, PrimRem] && width > 64 =
      one "div_word_steps" (words32 width * words32 width)
  | op `elem` [PrimQuot, PrimRem] = one "scalar_ops" 12
  | op `elem` [PrimExtract, PrimConcat, PrimTrunc, PrimZeroExt, PrimSignExt] =
      if out <= 64 then one "scalar_ops" 1 else one "copy_words" (words32 out)
  | op `notElem` [PrimAdd, PrimSub, PrimAnd, PrimOr, PrimXor, PrimSL, PrimSRL,
                 PrimSRA, PrimInv, PrimNeg, PrimEQ, PrimULE, PrimULT, PrimSLE,
                 PrimSLT, PrimBNot, PrimBAnd, PrimBOr, PrimIf, PrimMux,
                 PrimPriMux, PrimCase] = one "unknown_ops" 1
  | width <= 64 = one "scalar_ops" 1
  | otherwise = one "word_ops" (words32 width)
  where out = bits ty
        width = maximum (out : map (bits . ae_type) xs)

-- ASDef names identify shared DAG nodes; visit each once per candidate group.
-- Inline expressions remain distinct occurrences. Re-running this on merged
-- groups naturally avoids charging their common definitions twice.
groupCost :: DefMap -> [ARule] -> Cost
groupCost defs rules = snd (walkMany S.empty roots) `plus` actions `plus`
                      one "schedule_checks" (fromIntegral (length rules))
  where
    roots = concat [arule_pred r : concatMap aact_args (arule_actions r) | r <- rules]
    actions = foldl' plus M.empty
      [case a of
         ACall {} -> one "method_calls" 1 `plus`
                     one "copy_words" (sum [words32 (bits (ae_type x)) | x <- drop 1 (aact_args a)])
         _ -> one "effects" 1
      | r <- rules, a <- arule_actions r]
    walkMany seen es = foldl' step (seen,M.empty) es
      where step (s,c) e = let (s',d) = walk s e in (s',c `plus` d)
    walk seen (ASDef _ i)
      | S.member i seen = (seen,M.empty)
      | otherwise = case M.lookup i defs of
          Just (ADef _ _ e _) -> walk (S.insert i seen) e
          Nothing -> (S.insert i seen,one "unknown_ops" 1)
    walk seen (APrim _ ty op es) = children seen (opCost ty op es) es
    walk seen (AMethCall _ _ _ es) = children seen (one "method_calls" 1) es
    walk seen (AMethValue {}) = (seen,one "method_calls" 1)
    walk seen (ATuple _ es) = children seen M.empty es
    walk seen (ATupleSel _ e _) = walk seen e
    walk seen (ANoInlineFunCall _ _ _ es) = children seen (one "unknown_ops" 1) es
    walk seen (AFunCall _ _ _ _ es) = children seen (one "unknown_ops" 1) es
    walk seen (ASAny _ (Just e)) = walk seen e
    walk seen (ASInt {}) = (seen,M.empty)
    walk seen (ASStr {}) = (seen,M.empty)
    walk seen (ASReal {}) = (seen,M.empty)
    walk seen (ASAny {}) = (seen,M.empty)
    walk seen (ASPort {}) = (seen,M.empty)
    walk seen (ASParam {}) = (seen,M.empty)
    walk seen _ = (seen,one "unknown_ops" 1)
    children seen cost es = let (s,c) = walkMany seen es in (s,cost `plus` c)

object :: [(String, Integer)] -> String
object entries = "{" ++ intercalate "," [show k ++ ":" ++ show v | (k,v) <- entries] ++ "}"

costScore :: Cost -> Integer
costScore cost = sum [w * M.findWithDefault 0 k cost | (k,w) <- weights]

costJSON :: Cost -> String
costJSON cost = "{\"score\":" ++ show score ++ ",\"features\":" ++ object features ++ "}"
  where features = [(k,M.findWithDefault 0 k cost) | (k,_) <- weights]
        score = costScore cost

modelJSON :: String
modelJSON = "{\"version\":3,\"protocol_version\":5,\"units\":\"relative_work\",\"activation\":\"all_rules_all_branches\","
         ++ "\"word_bits\":32,\"weights\":" ++ object weights ++ "}"

-- Batched v5 transmits only each worker's endpoints. Report both no-payload
-- and every-payload bounds without assuming an activation frequency.
bridgeJSON :: Int -> [(Integer,Int,Int)] -> String
bridgeJSON workers cuts = object
  [("workers",fromIntegral workers), ("boundary_channels",fromIntegral (length cuts)),
   ("min_bytes_all_workers_per_cycle",sum (map minimumBytes [0..workers-1])),
   ("max_bytes_all_workers_per_cycle",sum (map maximumBytes [0..workers-1])),
   ("max_bytes_per_worker_cycle",maximum (0:map maximumBytes [0..workers-1]))]
  where
    local g = [w | (w,a,b) <- cuts, g == a || g == b]
    minimumBytes g = 4 * (12 + 6 * fromIntegral (length (local g)))
    maximumBytes g = minimumBytes g + 4 * sum (map words32 (local g))
