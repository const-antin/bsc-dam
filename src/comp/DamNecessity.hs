-- Reuse BSC's scheduled firing definitions and SAT implication checker.
-- The solver only sees a bounded Boolean abstraction, never wide arithmetic.
module DamNecessity (wireRequirements) where

import ASyntax
import ASyntaxUtil (aAnd, aOrs, aNot, aType)
import SimPackage
import Id (mkIdWillFire, mk_homeless_id, getIdBaseString, getIdString, isIdCanFire)
import Flags (Flags)
import Error (ErrorHandle)
import Prim (PrimOp(..))
import qualified SAT
import qualified Data.Map as M
import qualified Data.Set as S
import Control.Monad.State.Strict
import Control.Monad (guard, foldM, when)
import System.Environment (lookupEnv)
import PPrint (ppReadable)

-- Every expanded node is named, preserving sharing in reconvergent graphs.
data Proof = Proof { cache :: M.Map AExpr AExpr, active :: S.Set AExpr,
                     definitions :: [ADef], serial :: Int }
type Build = StateT Proof Maybe

fresh :: Build AId
fresh = do
  s <- get
  guard (serial s < 2048)
  put s { serial = serial s + 1 }
  return (mk_homeless_id ("DAM_PROOF_" ++ show (serial s)))

abstract :: DefMap -> [AId] -> [AId] -> [ARule] -> AExpr -> Build AExpr
abstract defs wires inputs rules = expr
  where
    expr e = do
      s <- get
      case M.lookup e (cache s) of
        Just v -> return v
        Nothing -> do
          guard (e `S.notMember` active s)
          modify (\t -> t { active = S.insert e (active t) })
          rhs <- expand e
          i <- fresh
          let v = ASDef (ATBit 1) i
          modify (\t -> t { active = S.delete e (active t),
                             cache = M.insert e v (cache t),
                             definitions = ADef i (ATBit 1) rhs [] : definitions t })
          return v
    atom = do i <- fresh; return (ASPort (ATBit 1) i)
    firing r = case M.lookup (mkIdWillFire (arule_id r)) defs of
      Just _ -> ASDef (ATBit 1) (mkIdWillFire (arule_id r))
      Nothing -> arule_pred r
    writers i = [(r,c,vs) | r <- rules, ACall j m (c:vs) <- arule_actions r,
                             i == j, getIdBaseString m == "wset"]
    enable (r,c,_) = aAnd <$> expr (firing r) <*> expr c
    valid i = aOrs <$> mapM enable (writers i)
    expand (ASDef _ i) = case M.lookup i defs of
      Just (ADef _ _ e _) | isIdCanFire i -> do
        -- SimMakeCBlocks may add mutual-exclusion inhibitors later. Model
        -- those as an arbitrary extra gate, so this is an overapproximation.
        base <- expr e
        inhibitor <- atom
        return (aAnd base inhibitor)
      Just (ADef _ _ e _) -> expr e
      Nothing -> atom
    expand e@(ASInt {}) = return e
    expand (AMethValue t i m) = expr (AMethCall t i m [])
    expand (AMethCall _ i m [])
      | i `elem` inputs && getIdBaseString m `elem` ["notEmpty","i_notEmpty"] =
          return (ASPort (ATBit 1) (mk_homeless_id ("DAM_INPUT_" ++ getIdString i)))
      | i `elem` wires && getIdBaseString m == "whas" = valid i
      | i `elem` wires && getIdBaseString m == "wget" = do
          -- A one-bit RWire carries its enabled writer's value. With no
          -- writer, its retained/undefined data is unconstrained, not False.
          terms <- mapM (\w@(_,_,vs) -> case vs of
                    [v] | aType v == ATBit 1 -> aAnd <$> enable w <*> expr v
                    _ -> lift Nothing) (writers i)
          has <- valid i
          unknown <- atom
          return (aOrs (aAnd (aNot has) unknown : terms))
    expand (APrim i t op es)
      | t == ATBit 1 && all ((== ATBit 1) . aType) es &&
        op `elem` [PrimBAnd,PrimAnd,PrimBOr,PrimOr,PrimBNot,PrimInv,
                   PrimXor,PrimEQ,PrimIf] = APrim i t op <$> mapM expr es
    expand _ = atom

wireRequirements :: ErrorHandle -> Flags -> SimPackage -> [AId] ->
                    [(ARule,[AId])] -> IO (M.Map AId (S.Set AId))
wireRequirements errh flags pkg wires queries
  | null wires || sum (map (length . snd) queries) > 256 = return M.empty
  | otherwise = case runStateT build (Proof M.empty S.empty [] 0) of
      Nothing -> do
        debug <- lookupEnv "BSC_DAM_DEBUG_PROOF"
        when (debug == Just "1") (putStrLn "DAM necessity: abstraction limit or cycle")
        return M.empty -- unknown/cyclic/too large: no certificate
      Just (qs,s) -> do
        debug <- lookupEnv "BSC_DAM_DEBUG_PROOF"
        when (debug == Just "1") $ do
          putStrLn ("DAM necessity queries: " ++ ppReadable qs)
          putStrLn (ppReadable (definitions s))
        solver <- SAT.initSATState "DAM wire necessity" errh flags False
                                   (definitions s) []
        (result,_) <- foldM check (M.empty,solver) qs
        return result
  where
    inputs = S.toList (S.fromList (concatMap snd queries))
    defs = sp_local_defs pkg
    build = mapM (\(r,is) -> do
              p <- abstract defs wires inputs (sp_rules pkg)
                     (ASDef (ATBit 1) (mkIdWillFire (arule_id r)))
              return (arule_id r,p,is)) queries
    check (result,solver) (rid,p,is) = foldM (one rid p) (result,solver) is
    one rid p (result,solver) i = do
      -- checkBiImplication returns both directions; only firing => input
      -- is a valid necessity certificate. Unknown is never accepted.
      ((necessary,_),solver') <- SAT.checkBiImplication solver p
        (ASPort (ATBit 1) (mk_homeless_id ("DAM_INPUT_" ++ getIdString i)))
      return (if necessary then M.insertWith S.union rid (S.singleton i) result
                           else result, solver')
