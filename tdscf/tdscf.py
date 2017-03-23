import numpy as np
import scipy, os, time
import scipy.linalg
from tdscf import *
import func
from pyscf import gto, dft, scf, ao2mo
from tdfields import *
from cmath import *
from pyscf import lib
import ctypes


libtdscf = load_library( os.path.expanduser('~') +'/tdscf_pyscf/lib/tdscf/libtdscf')
libdft = lib.load_library('libdft')

FsPerAu = 0.0241888

class tdscf:
    """
    A general TDSCF object.
    Other types of propagations may inherit from this.

    By default it does
    """
    def __init__(self,the_scf_,prm=None,output = 'log.dat', prop_=True):
        """
        Args:
            the_scf an SCF object from pyscf (should probably take advantage of complex RKS already in PYSCF)
        Returns:
            Nothing.
        """
        #To be Sorted later
        self.Enuc = the_scf_.energy_nuc()#the_scf_.e_tot - dft.rks.energy_elec(the_scf_,the_scf_.make_rdm1())[0]
        self.eri3c = None
        self.eri2c = None
        self.n_aux = None
        self.muxo = None
        self.muyo = None
        self.muzo = None
        self.mu0 = None
        self.hyb = the_scf_._numint.hybrid_coeff(the_scf_.xc, spin=(the_scf_.mol.spin>0)+1)
        self.adiis = None
        self.Exc = None
        #Global numbers
        self.t = 0.0
        self.n_ao = None
        self.n_mo = None
        self.n_occ = None
        self.n_virt = None
        self.n_e = None
        #Global Matrices
        self.rho = None # Current MO basis density matrix. (the idempotent (0,1) kind)
        self.rhoM12 = None # For MMUT step
        self.F = None # (LAO x LAO)
        self.K = None
        self.J = None
        self.eigs = None # current fock eigenvalues.
        self.S = None # (ao X ao)
        self.C = None # (ao X mo)
        self.X = None # AO, LAO
        self.V = None # LAO, current MO
        self.H = None # (ao X ao)  core hamiltonian.
        self.B = None # for ee2
        self.log = []
        # Objects
        self.the_scf  = the_scf_
        self.mol = the_scf_.mol
        self.MyBO = None
        #if (params["Model"]=="TDHF_BO"):
        #    self.MyBO = BORKS(...)
        self.auxmol_set()
        self.params = dict()
        self.initialcondition(prm)
        self.field = fields(the_scf_, self.params)
        self.field.InitializeExpectation(self.rho, self.C)
        start = time.time()
        self.prop(output)
        end = time.time()
        print "Propagation time:", end - start
        return

    def Csetup(self):
        print "============================="
        n_occ = int(sum(self.the_scf.mo_occ)/2)
        libtdscf.SetupDFT(\
        ctypes.c_int(int(self.n_ao)),ctypes.c_int(n_occ),ctypes.c_int(int(self.n_aux)),\
        self.B.ctypes.data_as(ctypes.c_void_p))


    def auxmol_set(self,auxbas = "weigend"):
        print "GENERATING INTEGRALS"
        auxmol = gto.Mole()
        auxmol.atom = self.mol.atom
        auxmol.basis = auxbas
        auxmol.build()
        mol = self.mol
        nao = self.n_ao = mol.nao_nr()
        naux = self.n_aux = auxmol.nao_nr()
        #print nao
        #print naux
        atm, bas, env = gto.conc_env(mol._atm, mol._bas, mol._env, auxmol._atm, auxmol._bas, auxmol._env)
        eri3c = np.empty((nao,nao,naux))
        pi = 0
        for i in range(mol.nbas):
            pj = 0
            for j in range(mol.nbas):
                pk = 0
                for k in range(mol.nbas, mol.nbas+auxmol.nbas):
                    shls = (i, j, k)
                    buf = gto.getints_by_shell('cint3c2e_sph', shls, atm, bas, env)
                    di, dj, dk = buf.shape
                    eri3c[pi:pi+di,pj:pj+dj,pk:pk+dk] = buf
                    pk += dk
                pj += dj
            pi += di
        print "ERI3C INTEGRALS GENERATED"
        eri2c = np.empty((naux,naux))
        pk = 0
        for k in range(mol.nbas, mol.nbas+auxmol.nbas):
            pl = 0
            for l in range(mol.nbas, mol.nbas+auxmol.nbas):
                shls = (k, l)
                buf = gto.getints_by_shell('cint2c2e_sph', shls, atm, bas, env)
                dk, dl = buf.shape
                eri2c[pk:pk+dk,pl:pl+dl] = buf
                pl += dl
            pk += dk
        print "ERI2C INTEGRALS GENERATED"
        self.eri3c = eri3c
        self.eri2c = eri2c
        RSinv = MatrixPower(eri2c,-0.5)
        self.B = np.einsum('ijp,pq->ijq', self.eri3c, RSinv) # (AO,AO,n_aux)
        return

    def FockBuild(self,P,it = -1):
        """
        Updates self.F given current self.rho (both complex.)
        Fock matrix with HF
        Args:
            P = LAO density matrix.
        Returns:
            Fock matrix(lao) . Updates self.F
        """
        # if (self.params["Model"]=="TDHF_BO"):
        #     return MyBO.FockBuild_BO(P) # transforms P into
        if self.params["Model"] == "TDHF":
            Pt = 2.0*TransMat(P,self.X,-1)
            J,K = self.get_jk(Pt)
            Veff = 0.5*(J+J.T.conj()) - 0.5*(0.5*(K + K.T.conj()))
            if self.adiis and it > 0:
                return TransMat(self.adiis.update(self.S,Pt,self.H + Veff),self.X)
            else:
                return  TransMat(self.H + 0.5*(J+J.T.conj()) - 0.5*(0.5*(K + K.T.conj())),self.X)
        elif self.params["Model"] == "TDDFT":
            Pt = 2 * TransMat(P,self.X,-1)
            #Veff = self.get_j(Pt)
            self.J = self.get_j_c(Pt)
            Veff = self.J.astype(complex)
            #Veff = self.the_scf.get_j(self.the_scf.mol,Pt)
            Veff += self.get_vxc(Pt)
            #Veff = J + Vxc
            if self.adiis and it > 0:
                return TransMat(self.adiis.update(self.S,Pt,self.H + Veff),self.X)
            else:
                return TransMat(self.H + Veff,self.X)


    def get_vxc(self,P):
        '''
        Args:
            P: AO density matrix

        Returns:
            Vxc: Exchange and Correlation matrix (AO)
        '''
        Vxc = self.numint_vxc(self.the_scf._numint,P)
        if(self.hyb > 0.01):
            #K = self.get_k(P)
            K = self.get_k_c(P)
            self.K = K
            Vxc += -0.5 * self.hyb * K
        return Vxc

    def numint_vxc(self,ni,P,max_mem = 2000):
        xctype = self.the_scf._numint._xc_type(self.the_scf.xc)
        make_rho, nset, nao = self._gen_rho_evaluator(self.mol, P, 1)
        ngrids = len(self.the_scf.grids.weights)
        non0tab = self.the_scf._numint.non0tab
        vmat = np.zeros((nset,nao,nao)).astype(complex)
        excsum = np.zeros(nset)

        if xctype == 'LDA':
            ao_deriv = 0
            for ao, mask, weight, coords in ni.block_loop(self.mol, self.the_scf.grids, nao, ao_deriv, max_mem, non0tab):
                rho = make_rho(0, ao, mask, 'LDA')
                exc, vxc = ni.eval_xc(self.the_scf.xc, rho.real, 0, 0, 1, None)[:2]
                vrho = vxc[0]
                den = rho * weight
                excsum[0] += (den * exc).sum()
                aow = np.einsum('pi,p->pi', ao, .5*weight*vrho)
                vmat += func._dot_ao_ao(self.mol, ao, aow, nao, weight.size, mask)#r_dot_product(ao[0].T,aow)
                rho = exc = vxc = vrho = aow = None
        elif xctype == 'GGA':
            ao_deriv = 1
            for ao, mask, weight, coords in ni.block_loop(self.mol, self.the_scf.grids, nao, ao_deriv, max_mem, non0tab):
                ngrid = weight.size
                rho = make_rho(0, ao, mask, 'GGA') # rho should be real
                exc, vxc = ni.eval_xc(self.the_scf.xc, rho.real, 0, 0, 1, None)[:2]
                vrho, vsigma = vxc[:2]
                wv = np.empty((4,ngrid))#.astype(complex)
                wv[0]  = weight * vrho * .5
                wv[1:] = rho[1:] * (weight * vsigma * 2)
                aow = np.einsum('npi,np->pi', ao, wv)
                vmat += func._dot_ao_ao(self.mol, ao[0], aow, nao, ngrid, mask)#r_dot_product(ao[0].T,aow) #np.einsum('ij,jk->ik',ao[0].T,aow) #_dot_ao_ao(self.mol, ao[0], aow, nao, ngrid, mask)
                den = rho[0] * weight
                excsum[0] += (den * exc).sum()
                # print weight.shape
                # print vrho.shape
                # print wv[0].shape
                #
                # print rho[1:].shape
                # print vsigma.shape
                # print wv[1:].shape
                #
                # print rho[0].shape
                # print den.shape
                # print exc.shape
                # quit()
                rho = exc = vxc = vrho = vsigma = wv = aow = None
        else:
            assert(all(x not in xc_code.upper() for x in ('CC06', 'CS', 'BR89', 'MK00')))
            ao_deriv = 2
            for ao, mask, weight, coords in ni.block_loop(self.mol, self.the_scf.grids, nao, ao_deriv, max_mem, non0tab):
                ngrid = weight.size
                rho = make_rho(0, ao, mask, 'MGGA')
                exc, vxc = ni.eval_xc(self.the_scf.xc, rho.real, 0, 0, 1, None)[:2]
                vrho, vsigma, vlapl, vtau = vxc[:4]
                den = rho[0] * weight
                excsum[0] += (den * exc).sum()
                wv = np.empty((4,ngrid))
                wv[0]  = weight * vrho * .5
                wv[1:] = rho[1:4] * (weight * vsigma * 2)
                aow = np.einsum('npi,np->pi', ao[:4], wv)
                vmat += func._dot_ao_ao(mol, ao[0], aow, nao, ngrid, mask)
                wv = (.5 * .5 * weight * vtau).reshape(-1,1)
                vmat += func._dot_ao_ao(mol, ao[1], wv*ao[1], nao, ngrid, mask)#r_dot_product(ao[1].T,wv*ao[1])
                vmat += func._dot_ao_ao(mol, ao[2], wv*ao[2], nao, ngrid, mask)
                vmat += func._dot_ao_ao(mol, ao[3], wv*ao[3], nao, ngrid, mask)
                rho = exc = vxc = vrho = vsigma = wv = aow = None
        self.Exc = excsum[0]

        Vxc = vmat.reshape(nao,nao)
        Vxc = Vxc + Vxc.T.conj()

        return Vxc

    def _gen_rho_evaluator(self, mol, dms, hermi=1):
        natocc = []
        natorb = []
        # originally scipy
        e, c = scipy.linalg.eigh(dms)
        natocc.append(e)
        natorb.append(c)
        nao = dms.shape[0]
        ndms = len(natocc)
        def make_rho(idm, ao, non0tab, xctype):
            return eval_rhoc(mol, ao, natorb[idm], natocc[idm], non0tab, xctype)
        return make_rho, ndms, nao

    def get_jk_c(self, P):
        '''
        Args:
            P: AO density matrix
        Returns:
            J: Coulomb matrix
            K: Exchange matrix
        '''
        return self.get_j_c(P), self.get_k_c(P)

    def get_jk(self, P):
        '''
        Args:
            P: AO density matrix
        Returns:
            J: Coulomb matrix
            K: Exchange matrix
        '''
        return self.get_j(P), self.get_k(P)

    def get_j_c(self,P):
        '''
        Args:
            P: AO density matrix

        Returns:
            J: Coulomb matrix (AO)
        '''
        Pc = np.asarray(P, order='C').astype(complex)
        Jmat = np.zeros((self.n_ao,self.n_ao))
        libtdscf.get_j(\
        Pc.ctypes.data_as(ctypes.c_void_p), Jmat.ctypes.data_as(ctypes.c_void_p))
        return Jmat

    def get_k_c(self,P):
        '''
        Args:
            P: AO density matrix
        Returns:
            K: Exchange matrix (AO)
        '''
        Pc = np.asarray(P, order='C').astype(complex)
        Kmat = np.zeros((self.n_ao,self.n_ao)).astype(complex)
        #Kmat = np.asarray(Kmat,order = 'C')
        libtdscf.get_k(\
        Pc.ctypes.data_as(ctypes.c_void_p), Kmat.ctypes.data_as(ctypes.c_void_p))
        return Kmat.T

    def get_j(self,P):
        '''
        Args:
            P: AO density matrix

        Returns:
            J: Coulomb matrix (AO)
        '''
        naux = self.n_aux
        nao = self.n_ao
        rho = np.einsum('ijp,ij->p', self.eri3c, P)
        rho = np.linalg.solve(self.eri2c, rho)
        jmat = np.einsum('p,ijp->ij', rho, self.eri3c)
        #print "jmat\n",jmat
        return jmat

    def get_k(self,P):
        '''
        Args:
            P: AO density matrix
        Returns:
            K: Exchange matrix (AO)
        '''
        naux = self.n_aux
        nao = self.n_ao
        kpj = np.einsum('ijp,jk->ikp', self.eri3c, P)
        pik = np.linalg.solve(self.eri2c, kpj.reshape(-1,naux).T.conj())
        rkmat = np.einsum('pik,kjp->ij', pik.reshape(naux,nao,nao), self.eri3c)
        return rkmat

    def initialcondition(self,prm):
        print '''
        ===================================
        |  Realtime TDSCF module          |
        ===================================
        | J. Parkhill, T. Nguyen          |
        | J. Koh, J. Herr,  K. Yao        |
        ===================================
        | Refs: 10.1021/acs.jctc.5b00262  |
        |       10.1063/1.4916822         |
        ===================================
        '''
        n_ao = self.n_ao = self.the_scf.make_rdm1().shape[0]
        n_mo = self.n_mo = n_ao # should be fixed.
        n_occ = self.n_occ = int(sum(self.the_scf.mo_occ)/2)
        self.Csetup()
        self.n_virt = self.n_mo - self.n_occ
        print "n_ao:", n_ao, "n_mo:", n_mo, "n_occ:", n_occ
        self.ReadParams(prm)
        self.InitializeLiouvillian()
        return

    def ReadParams(self,prm):
        '''
        Set Defaults, Read the file and fill the params dictionary
        '''
        self.params["Model"] = "TDDFT" #"TDHF"; the difference of Fock matrix and energy
        self.params["Method"] = "MMUT"#"MMUT"
        self.params["BBGKY"]=0
        self.params["TDCIS"]=1

        self.params["dt"] =  0.02
        self.params["MaxIter"] = 15000

        self.params["ExDir"] = 1.0
        self.params["EyDir"] = 1.0
        self.params["EzDir"] = 1.0
        self.params["FieldAmplitude"] = 0.01
        self.params["FieldFreq"] = 0.9202
        self.params["Tau"] = 0.07
        self.params["tOn"] = 7.0*self.params["Tau"]
        self.params["ApplyImpulse"] = 1
        self.params["ApplyCw"] = 0

        self.params["StatusEvery"] = 5000
        self.params["Print"]=0
        # Here they should be read from disk.
        if(prm != None):
            for line in prm.splitlines():
                s = line.split()
                if len(s) > 1:
                    if s[0] == "MaxIter" or s[0] == str("ApplyImpulse") or s[0] == str("ApplyCw") or s[0] == str("StatusEvery"):
                        self.params[s[0]] = int(s[1])
                    elif s[0] == "Model" or s[0] == "Method":
                        self.params[s[0]] = s[1]
                    else:
                        self.params[s[0]] = float(s[1])

        print "============================="
        print "         Parameters"
        print "============================="
        print "Model:", self.params["Model"]
        print "Method:", self.params["Method"]
        print "dt:", self.params["dt"]
        print "MaxIter:", self.params["MaxIter"]
        print "ExDir:", self.params["ExDir"]
        print "EyDir:", self.params["EyDir"]
        print "EzDir:", self.params["EzDir"]
        print "FieldAmplitude:", self.params["FieldAmplitude"]
        print "FieldFreq:", self.params["FieldFreq"]
        print "Tau:", self.params["Tau"]
        print "tOn:", self.params["tOn"]
        print "ApplyImpulse:", self.params["ApplyImpulse"]
        print "ApplyCw:", self.params["ApplyCw"]
        print "StatusEvery:", self.params["StatusEvery"]
        print "=============================\n\n"

        return

    def InitializeLiouvillian(self):
        '''
        Get an initial Fock matrix.
        '''
        S = self.S = self.the_scf.get_ovlp()
        self.X = MatrixPower(S,-1./2.)
        self.V = np.eye(self.n_ao)
        #self.X = scipy.linalg.fractional_matrix_power(S,-1./2.)
        self.C = self.X.copy() # Initial set of orthogonal coordinates.
        self.InitFockBuild() # updates self.C
        self.rho = 0.5*np.diag(self.the_scf.mo_occ).astype(complex)
        self.rhoM12 = self.rho.copy()
        return

    def InitFockBuild(self):
        '''
        Using Roothan's equation to build a Fock matrix and initial density matrix
        Returns:
            self consistent density in Lowdin basis.
        '''
        start = time.time()
        n_occ = self.n_occ
        Ne = self.n_e = 2.0 * n_occ
        err = 100
        it = 0
        self.H = self.the_scf.get_hcore()
        S = self.S.copy()
        SX = np.dot(S,self.X)
        Plao = 0.5*TransMat(self.the_scf.get_init_guess(self.mol, self.the_scf.init_guess), SX).astype(complex)
        adiis = self.the_scf.DIIS(self.the_scf, self.the_scf.diis_file)
        adiis.space = self.the_scf.diis_space
        adiis.rollback = self.the_scf.diis_space_rollback
        self.adiis = adiis

        self.F = self.FockBuild(Plao)
        Plao_old = Plao
        E = self.energy(Plao)+ self.Enuc

        # if (self.params["Model"] == "TDHF"):
        while (err > 10**-10):
            # Diagonalize F in the lowdin basis
            self.eigs, self.V = np.linalg.eig(self.F)
            idx = self.eigs.argsort()
            self.eigs.sort()
            self.V = self.V[:,idx].copy()
            # Fill up the density in the MO basis and then Transform back
            Pmo = 0.5*np.diag(self.the_scf.mo_occ).astype(complex)
            Plao = TransMat(Pmo,self.V,-1)
            print "Ne", np.trace(Plao), np.trace(Pmo)
            Eold = E
            E = self.energy(Plao)
            self.F = self.FockBuild(Plao,it)
            err = abs(E-Eold)
            if (it%1 ==0):
                print "Iteration:", it,"; Energy:",E,"; Error =",err
            it += 1
        #print "exc: ",self.the_scf._exc, "; ecoul: ",self.the_scf._ecoul
        Pmo = 0.5*np.diag(self.the_scf.mo_occ).astype(complex)
        Plao = TransMat(Pmo,self.V,-1)
        self.C = np.dot(self.X,self.V)
        self.rho = TransMat(Plao,self.V)
        self.rhoM12 = TransMat(Plao,self.V)
        print "Ne", np.trace(Plao), np.trace(self.rho),
        print "Energy:",E
        print "Initial Fock Matrix(MO)\n", TransMat(self.F,self.V)
        end = time.time()
        print "Initial Fock Built time:",end-start
        #quit()
        return Plao

    def Split_RK4_Step_MMUT(self, w, v , oldrho , time, dt ,IsOn):
        Ud = np.exp(w*(-0.5j)*dt);
        U = TransMat(np.diag(Ud),v,-1)
        RhoHalfStepped = TransMat(oldrho,U,-1)
        # If any TCL propagation occurs...
        DontDo="""
        SplitLiouvillian( RhoHalfStepped, k1,tnow,IsOn);
        v2 = (dt/2.0) * k1;
        v2 += RhoHalfStepped;
        SplitLiouvillian(  v2, k2,tnow+(dt/2.0),IsOn);
        v3 = (dt/2.0) * k2;
        v3 += RhoHalfStepped;
        SplitLiouvillian(  v3, k3,tnow+(dt/2.0),IsOn);
        v4 = (dt) * k3;
        v4 += RhoHalfStepped;
        SplitLiouvillian(  v4, k4,tnow+dt,IsOn);
        newrho = RhoHalfStepped;
        newrho += dt*(1.0/6.0)*k1;
        newrho += dt*(2.0/6.0)*k2;
        newrho += dt*(2.0/6.0)*k3;
        newrho += dt*(1.0/6.0)*k4;
        newrho = U*newrho*U.t();
        """
        newrho = TransMat(RhoHalfStepped,U,-1)
#        print U.dot(U.T.conj())
#        print newrho
        return newrho

    def rhodot(self,rho,time,F):
        '''
        Args:
            rho: LAO basis density
            time: au
            F: Orig. MO basis fock.
        Returns:
            kt: rhodot(MO)
        '''
        raise Exception("Not fixed")
        print "F",F
        print "Rho", rho
        Ft, IsOn = self.field.ApplyField(F,self.C, time)
        tmp = -1.j*(np.dot(Ft,rho) - np.dot(rho,Ft))
        print "Ft",Ft
        print "tmp[0,1]", tmp[0,1], tmp[1,0], tmp[0,1]/rho[0,1]
        return -1.j*(np.dot(Ft,rho) - np.dot(rho,Ft))

    def TDDFTstep(self,time):
        #self.rho (MO basis)
        if (self.params["Method"] == "MMUT"):
            self.F = self.FockBuild(TransMat(self.rho,self.V,-1)) # is LAO basis
            self.F = np.conj(self.F)
            Fmo_prev = TransMat(self.F, self.V)
            self.eigs, rot = np.linalg.eig(Fmo_prev)
            # print Fmo_prev, rot
            # Rotate all the densities into the current fock eigenbasis.
            #roti = np.linalg.inv(rot)
            self.rho = TransMat(self.rho, rot)
            self.rhoM12 = TransMat(self.rhoM12, rot)
            self.V = np.dot(self.V , rot)
            self.C = np.dot(self.X , self.V)
            # propagation is done in the current eigenbasis.
            Fmo = np.diag(self.eigs).astype(complex)
            #Fmo = TransMat(self.F,self.V)
            # Check that the energy is still correct.
            Hmo = TransMat(self.H,self.C)
            FmoPlusField, IsOn = self.field.ApplyField(Fmo,self.C,time)
            #print "Fmo + FIeld\n",FmoPlusField
            w,v = scipy.linalg.eig(FmoPlusField)
            NewRhoM12 = self.Split_RK4_Step_MMUT(w, v, self.rhoM12, time, self.params["dt"], IsOn)
            NewRho = self.Split_RK4_Step_MMUT(w, v, NewRhoM12, time,self.params["dt"]/2.0, IsOn)
            self.rho = 0.5*(NewRho+(NewRho.T.conj()));
            self.rhoM12 = 0.5*(NewRhoM12+(NewRhoM12.T.conj()))
        elif (self.params["Method"] == "RK4"):
            raise Exception("Broken.")
            dt = self.params["dt"]
            rho = self.rho.copy()
            C = self.C.copy()
            # First perform a fock build
            # MO -> AO requires C
            self.FockBuild( TransMat(rho,C, -1) )
            #Fmo = TransMat(self.F,self.Cinv,-1) # AO -> MO requires Cinv NOT A NORM CONSERVING TRANSFORMATION, HENCE WRONG EIGVALUES.
            Fmo = TransMat(self.F,C)# C will F(AO) to F(MO)
            if (1):
                eigs, rot = np.linalg.eig(Fmo)
                print "Eigs", eigs

            k1 = self.rhodot(rho,time,Fmo)
            v2 = (0.5 * dt)*k1
            v2 += rho
            #print Fmo
            self.FockBuild( TransMat(v2, C, -1) )
            Fmo = TransMat(self.F,C)
            k2 = self.rhodot(v2, time + 0.5*dt,Fmo)
            v3 = (0.5 * dt)*k2
            v3 += rho

            self.FockBuild( TransMat(v3, C, -1) )
            Fmo = TransMat(self.F,C)
            k3 = self.rhodot(v3, time + 0.5*dt,Fmo)
            v4 = (1.0 * dt)*k3
            v4 += rho

            self.FockBuild( TransMat(v4, C, -1) )
            Fmo = TransMat(self.F,C)
            k4 = self.rhodot(v4, time + dt,Fmo)

            drho = dt/6.0 * (k1 + 2.0*k2 + 2.0*k3 + k4)
            self.rho += drho
            self.rho = 0.5 * (self.rho + self.rho.T.conj())
            #print self.F
            #print Fmo
            #print "t",time, "\n",self.rho
            #print np.trace(self.rho)
        else:
            raise Exception("Unknown Method...")
        return

    def EE2step(self,time):
        if (self.params["Method"] == "MMUT"):
            libtdscf.MMUT_step(self.rho.ctypes.data_as(ctypes.c_void_p), self.rhoM12.ctypes.data_as(ctypes.c_void_p), ctypes.c_double(time))
            # Rho and RhoM12 is updated
        elif (self.params["Method"] == "RK4"):
            #
            newrho = self.rho.copy()
            libtdscf.TDTDAstep(newrho.ctypes.data_as(ctypes.c_void_p), ctypes.c_double(time))
            self.rho = newrho.copy()
            #print self.rho
        #quit()
        return

    def step(self,time):
        """
        Performs a step
        Updates t, rho, and possibly other things.
        """
        if (self.params["Model"] == "TDDFT" or self.params["Model"] == "TDHF"):
            return self.TDDFTstep(time)
        elif(self.params["Model"] == "EE2"):
            return self.EE2step(time)
        return

    def dipole(self):
        return self.field.Expectation(self.rho, self.C)

    def energy(self,Plao,IfPrint=False):
        """
        P: Density in LAO basis.
        """
        if (self.params["Model"] == "TDHF" or self.params["Model"] == "BBGKY"):
            Hlao = TransMat(self.H,self.X)
            return (self.Enuc+np.trace(np.dot(Plao,Hlao+self.F))).real
        elif self.params["Model"] == "TDDFT":
            Hlao = TransMat(self.H,self.X)
            P = TransMat(Plao,self.X,-1)
            #P = 0.5*Plao
            J = self.J.copy()
            #J = self.the_scf.get_j(self.mol,2 * P.real)
            Exc = self.Exc #self.nr_rks_energy(self.the_scf._numint,self.mol, self.the_scf.grids, self.the_scf.xc, 2*P, 1)
            if(self.hyb > 0.01):
                Exc -= 0.5 * self.hyb * TrDot(P,self.K)
            # if not using auxmol
            #Exc -= 0.5 * self.hyb * TrDot(P,self.the_scf.get_k(self.mol,2 * P.real)) # requires real only()
            EH = TrDot(Plao,2*Hlao)
            #EH = TrDot(P,2*self.H)
            EJ = TrDot(P,J)
            E = EH + EJ + Exc + self.Enuc
            #print "ONE e",EH.real
            #print "COLOUM",EJ.real
            #print "EXC",Exc.real
            #print "Nuclear", self.Enuc

            return E.real

    def loginstant(self,iter):
        """
        time is logged in atomic units.
        """
        np.set_printoptions(precision = 7)
        tore = str(self.t)+" "+str(self.dipole().real).rstrip(']').lstrip('[')+ " " +str(self.energy(TransMat(self.rho,self.V,-1),False))+" "+str(np.trace(self.rho))

        #tore = str(self.t)+" "+str(np.sin(0.5*np.ones(3)*self.t)).rstrip(']').lstrip('[]')+ " " +str(self.energyc(TransMat(self.rho,self.C,-1),False)+self.Enuc)
        #print tore
        if iter%self.params["StatusEvery"] ==0 or iter == self.params["MaxIter"]-1:
            print 't:', self.t*FsPerAu, " (Fs)  Energy:",self.energy(TransMat(self.rho,self.V,-1)), " Tr ",(np.trace(self.rho))
            print('Dipole moment(X, Y, Z, au): %8.5f, %8.5f, %8.5f' %(self.dipole().real[0],self.dipole().real[1],self.dipole().real[2]))
        return tore

    def prop(self,output):
        """
        The main tdscf propagation loop.
        """
        iter = 0
        self.t = 0
        f = open(output,'a')
        print "Energy Gap (eV)",abs(self.eigs[self.n_occ]-self.eigs[self.n_occ-1])*27.2114
        print "\n\nPropagation Begins"
        start = time.time()
        while (iter<self.params["MaxIter"]):
            self.step(self.t)
            #print self.t
            #self.log.append(self.loginstant(iter))
            f.write(self.loginstant(iter)+"\n")
            # Do logging.
            self.t = self.t + self.params["dt"]
            if iter%self.params["StatusEvery"] ==0 or iter == self.params["MaxIter"]-1:
                end = time.time()
                print (end - start)/(60*60*self.t * FsPerAu * 0.001), "hr/ps"
            iter = iter + 1

        f.close()

# def _dot_ao_ao(mol, ao1, ao2, nao, ngrids, non0tab):
#     '''return numpy.dot(ao1.T, ao2)'''
#     natm = ctypes.c_int(mol._atm.shape[0])
#     nbas = ctypes.c_int(mol.nbas)
#     ao1 = np.asarray(ao1, order='C')
#     ao2 = np.asarray(ao2, order='C')
#     vv = np.empty((nao,nao))
#     libdft.VXCdot_ao_ao(vv.ctypes.data_as(ctypes.c_void_p),
#                         ao1.ctypes.data_as(ctypes.c_void_p),
#                         ao2.ctypes.data_as(ctypes.c_void_p),
#                         ctypes.c_int(nao), ctypes.c_int(ngrids),
#                         ctypes.c_int(BLKSIZE),
#                         non0tab.ctypes.data_as(ctypes.c_void_p),
#                         mol._atm.ctypes.data_as(ctypes.c_void_p), natm,
#                         mol._bas.ctypes.data_as(ctypes.c_void_p), nbas,
#                         mol._env.ctypes.data_as(ctypes.c_void_p))
#     return vv
