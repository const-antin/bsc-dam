-- Typed handoff of compiler-validated, explicitly annotated contexts.
module DamPlan (DamPlan(..), writePlan) where
import System.Environment (lookupEnv)

data DamPlan = Plan {
    rules :: [[String]],
    channels :: [(String,Integer,Integer,Int,Int)],
    foreignOwners :: [Bool], foreignDecls :: [String],
    requiredContexts :: [(String,Int)], mainWorker :: Maybe Int
  } deriving (Read,Show,Eq)

writePlan :: DamPlan -> IO ()
writePlan plan = do
  path <- lookupEnv "BSC_DAM_PLAN"
  maybe (return ()) (\p -> writeFile p (show plan)) path
