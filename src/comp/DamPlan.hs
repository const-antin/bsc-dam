-- Typed compiler/driver handoff and static execution policy. No JSON parser or
-- application profiling is needed by the compiler.
module DamPlan (DamPlan(..), writePlan, chooseLayout, estimate) where
import System.Environment (lookupEnv)
import Data.List (nub, sort)

data DamPlan = Fallback String | Plan {
    rules :: [[String]], costs :: [Integer], total :: Integer,
    channels :: [(String,Integer,Integer,Int,Int)],
    foreignOwners :: [Bool], foreignDecls :: [String],
    requiredContexts :: [(String,Int)]
  } deriving (Read,Show,Eq)

writePlan :: DamPlan -> IO ()
writePlan plan = do
  path <- lookupEnv "BSC_DAM_PLAN"
  maybe (return ()) (\p -> writeFile p (show plan)) path

estimate :: DamPlan -> [Int] -> Integer -> Integer -> Integer
estimate p labels ipc overhead = maximum (0 : groupCosts) +
  sum [overhead + ipc * wireWords g | g <- groups]
  where
    groups = sort (nub labels)
    groupCosts = [sum [c | (c,l) <- zip (costs p) labels, l == g] | g <- groups]
    -- Sparse local protocol: request/reply headers plus length prefixes are
    -- twelve words (including the batch horizon). Each local endpoint has three words in either direction;
    -- its payload crosses the process boundary once, in its owner's direction.
    -- Charge every payload as valid: no sparse rule-activation assumption.
    cuts = [(w,labels !! a,labels !! b) | (_,w,_,a,b) <- channels p,
             labels !! a /= labels !! b]
    wireWords g = 12 + sum [6 + (w+31) `div` 32 | (w,a,b) <- cuts, g == a || g == b]

chooseLayout :: DamPlan -> Int -> Integer -> Integer -> Integer ->
                (Maybe [Int], String, Integer)
chooseLayout p workers ipc overhead gain
  | length (rules p) > 64 = (Nothing,"bounded search declined more than 64 groups",0)
  | null candidates = (Nothing,"worker limit leaves ordinary Bluesim",0)
  | null (requiredContexts p) && total p * 100 < score * gain = (Nothing,"estimated work does not cover communication overhead",score)
  | otherwise = (Just normalized,if null (requiredContexts p)
                      then "static estimate exceeds minimum gain"
                      else "required dam_context boundaries preserved",score)
  where
    -- A cheap group cannot amortize even the fixed worker overhead. Merge
    -- it into a FIFO neighbor before the balanced-cut search; otherwise the
    -- cheapest cut can isolate a nearly empty prefetch/controller process.
    -- Preserve two candidates so forced execution with a worker cap can still
    -- run a legal split; automatic mode rejects unprofitable tiny designs.
    locked ls g = any (\(_,i) -> ls !! i == g) (requiredContexts p)
    mergeable ls a b = not (locked ls a || locked ls b)
    candidates = explore (coalesce [0 .. length (rules p)-1])
    coalesce ls = case cheapMerges of
      [] -> ls
      _ -> coalesce (snd (minimum [(value xs,xs) | xs <- cheapMerges]))
      where
        gs = sort (nub ls)
        cheap = [g | g <- gs, sum [c | (c,l) <- zip (costs p) ls, l == g] < overhead + 12*ipc]
        adjacent a b = any (\(_,_,_,x,y) -> (ls !! x == a && ls !! y == b) ||
                                              (ls !! x == b && ls !! y == a)) (channels p)
        cheapMerges = [[if x == a then b else x | x <- ls] |
                        length gs > 2, a <- cheap, b <- gs, a /= b, adjacent a b, mergeable ls a b]
    explore ls | length gs < 2 = []
               | otherwise = [(value ls,ls) | length gs <= workers] ++ if null merges then [] else explore best
      where
        gs = sort (nub ls)
        merges = [[if x == b then a else x | x <- ls] | a <- gs, b <- gs, a < b, mergeable ls a b]
        best = snd (minimum [(value xs,xs) | xs <- merges])
    value ls = estimate p ls ipc overhead
    (score,labels) = minimum candidates
    normalized = [length (filter (< x) (sort (nub labels))) | x <- labels]
