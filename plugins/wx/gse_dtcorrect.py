#!/usr/bin/env python
"""
"""
import os
import time
import shutil
import numpy as np
from random import randrange
from functools import partial
from datetime import timedelta

import wx
import wx.lib.agw.flatnotebook as flat_nb
import wx.lib.scrolledpanel as scrolled
import wx.lib.mixins.inspection

HAS_EPICS = False
try:
    import epics
    from epics.wx import DelayedEpicsCallback, EpicsFunction
    HAS_EPICS = True
except ImportError:
    pass
    
from larch import Interpreter, use_plugin_path
from larch.larchlib import read_workdir, save_workdir
        
use_plugin_path('io')
from gse_escan import gsescan_deadtime_correct
from gse_xdiscan import gsexdi_deadtime_correct, is_GSEXDI

from wxutils import (SimpleText, FloatCtrl, pack, Button, Popup,
                     Choice,  Check, MenuItem, GUIColors,
                     CEN, RCEN, LCEN, FRAMESTYLE, Font)

CEN |=  wx.ALL
FILE_WILDCARDS = "Scan Data Files(*.0*,*.dat,*.xdi)|*.0*;*.dat;*.xdi|All files (*.*)|*.*"
FNB_STYLE = flat_nb.FNB_NO_X_BUTTON|flat_nb.FNB_SMART_TABS|flat_nb.FNB_NO_NAV_BUTTONS


WORKDIR_FILE = 'dtc_file.txt' 

def okcancel(panel, onOK=None, onCancel=None):
    btnsizer = wx.StdDialogButtonSizer()
    _ok = wx.Button(panel, wx.ID_OK)
    _no = wx.Button(panel, wx.ID_CANCEL)
    panel.Bind(wx.EVT_BUTTON, onOK,     _ok)
    panel.Bind(wx.EVT_BUTTON, onCancel, _no)
    _ok.SetDefault()
    btnsizer.AddButton(_ok)
    btnsizer.AddButton(_no)
    btnsizer.Realize()
    return btnsizer

    
class DTCorrectFrame(wx.Frame):
    _about = """GSECARS Deadtime Corrections
  Matt Newville <newville @ cars.uchicago.edu>
  """
    def __init__(self, _larch=None, **kws):

        wx.Frame.__init__(self, None, -1, style=FRAMESTYLE)
        self.file_groups = {}
        self.file_paths  = []
        title = "DeadTime Correction "
        self.larch = _larch
        self.subframes = {}
       
        self.SetSize((380, 180))
        self.SetFont(Font(10))

        self.config = {'chdir_on_fileopen': True}
        self.SetTitle(title)
        self.createMainPanel()
        self.createMenus()
        self.statusbar = self.CreateStatusBar(2, 0)
        self.statusbar.SetStatusWidths([-3, -1])
        statusbar_fields = ["Initializing....", " "]
        for i in range(len(statusbar_fields)):
            self.statusbar.SetStatusText(statusbar_fields[i], i)
        read_workdir(WORKDIR_FILE)
        
    def onBrowse(self, event=None):
        dlg = wx.FileDialog(parent=self, 
                        message='Select Files',
                        defaultDir=os.getcwd(),
                        wildcard =FILE_WILDCARDS,
                        style=wx.OPEN|wx.MULTIPLE|wx.CHANGE_DIR)
        
        if dlg.ShowModal() == wx.ID_OK:
            path = dlg.GetPath()
            mdir, p = os.path.split(path)
            os.chdir(mdir)
            roiname = self.roi_wid.GetValue().strip()
            if len(roiname) < 1: 
                Popup(self, 
                    'Must give ROI name!', 'No ROI name')
                return
            dirname = self.dir_wid.GetValue().strip()
            if len(dirname) > 1 and not os.path.exists(dirname):
                try:
                    os.mkdir(dirname)
                except:
                    Popup(self, 
                        'Could not create directory %s' % dirname,
                        "could not create directory")
                    return
            for fname in dlg.GetFilenames():
                corr_fcn = gsescan_deadtime_correct
                if is_GSEXDI(fname):
                    corr_fcn = gsexdi_deadtime_correct
                corr_fcn(fname, roiname, subdir=dirname, _larch=self.larch)

    def createMainPanel(self):
        panel = wx.Panel(self)
        sizer = wx.GridBagSizer(5, 4)
 
        lab1 = SimpleText(panel, ' Element / ROI Name')
        lab2 = SimpleText(panel, ' Output Folder:')
        lab3 = SimpleText(panel, ' Select Files:')
        self.roi_wid = wx.TextCtrl(panel, -1, '', size=(200, -1))
        self.dir_wid = wx.TextCtrl(panel, -1, 'DT_Corrected', size=(200, -1))
        self.sel_wid = Button(panel, 'Browse', size=(100, -1),
                                action=self.onBrowse)
        
        ir = 0
        sizer.Add(lab1,         (ir, 0), (1, 1), LCEN, 2)
        sizer.Add(self.roi_wid, (ir, 1), (1, 1), LCEN, 2)
        ir += 1
        sizer.Add(lab2,          (ir, 0), (1, 1), LCEN, 2)
        sizer.Add(self.dir_wid,  (ir, 1), (1, 1), LCEN, 2)      
        ir += 1
        sizer.Add(lab3,          (ir, 0), (1, 1), LCEN, 2)
        sizer.Add(self.sel_wid,  (ir, 1), (1, 1), LCEN, 2)              

        pack(panel, sizer)
        wx.CallAfter(self.init_larch)
        return 

    def init_larch(self):
        t0 = time.time()
        if self.larch is None:
            self.larch = Interpreter()
        self.larch.symtable.set_symbol('_sys.wx.wxapp', wx.GetApp())
        self.larch.symtable.set_symbol('_sys.wx.parent', self)

        self.SetStatusText('ready')
        
    def write_message(self, s, panel=0):
        """write a message to the Status Bar"""
        self.SetStatusText(s, panel)

    def createMenus(self):
        # ppnl = self.plotpanel
        self.menubar = wx.MenuBar()
        #
        fmenu = wx.Menu()
        
        MenuItem(self, fmenu, "&Quit\tCtrl+Q", "Quit program", self.onClose)

        self.menubar.Append(fmenu, "&File")

      
        self.SetMenuBar(self.menubar)

    def onClose(self,evt):
        save_workdir(WORKDIR_FILE)
        self.Destroy()

  
class DTViewer(wx.App, wx.lib.mixins.inspection.InspectionMixin):
    def __init__(self, _larch=None, **kws):
        self._larch = _larch
        wx.App.__init__(self, **kws)

    def run(self):
        self.MainLoop()

    def createApp(self):
        frame = DTCorrectFrame(_larch=self._larch)
        frame.Show()
        self.SetTopWindow(frame)

    def OnInit(self):
        self.createApp()
        return True

def _dtcorrect(wxparent=None, _larch=None,  **kws):
    s = DTCorrectFrame(_larch=_larch, **kws)
    s.Show()
    s.Raise()


def registerLarchPlugin():
    return ('_plotter', {'dtcorrect_viewer':_dtcorrect})


if __name__ == '__main__':
    x = DTViewer()
    x.run()
