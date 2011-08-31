'''
   Plotting functions for Larch, wrapping the mplot plotting
   widgets which use matplotlib

Exposed functions here are
   plot:  display 2D line plot to an enhanced,
            configurable Plot Frame
   oplot: overplot a 2D line plot on an existing Plot Frame
   imshow: display a false-color map from array data on
           a configurable Image Display Frame.
'''
import time
import wx
from larch.mplot import PlotFrame, ImageFrame

IMG_DISPLAYS = {}
PLOT_DISPLAYS = {}
MODNAME = '_plotter'

def ensuremod(larch):
    if larch is not None:
        symtable = larch.symtable
        if not symtable.has_group(MODNAME):
            symtable.newgroup(MODNAME)
        return symtable

class PlotDisplay(PlotFrame):
    def __init__(self, wxparent=None, window=1, larch=None, **kws):
        PlotFrame.__init__(self, parent=wxparent,
                                 exit_callback=self.onExit, **kws)
        self.Show()
        self.Raise()
        self.cursor_pos = []
        self.panel.cursor_callback = self.onCursor
        self.window = int(window)
        self.larch = larch
        self.symname = '%s.plot%i' % (MODNAME, self.window)
        symtable = ensuremod(self.larch)
        if symtable is not None:
            symtable.set_symbol(self.symname, self)
        if window not in PLOT_DISPLAYS:
            PLOT_DISPLAYS[window] = self

    def onExit(self, o, **kw):
        try:
            symtable = self.larch.symtable
            if symtable.has_group(MODNAME):
                symtable.del_symbol(self.symname)
        except:
            pass
        if self.window in PLOT_DISPLAYS:
            PLOT_DISPLAYS.pop(self.window)

        self.Destroy()

    def onCursor(self,x=None, y=None,**kw):
        symtable = ensuremod(self.larch)
        if symtable is None:

            return
        symtable.set_symbol('%s_x'  % self.symname, x)
        symtable.set_symbol('%s_y'  % self.symname, y)


class ImageDisplay(ImageFrame):
    def __init__(self, wxparent=None, window=1, larch=None, **kws):
        ImageFrame.__init__(self, parent=wxparent,
                                  exit_callback=self.onExit, **kws)
        self.Show()
        self.Raise()
        self.cursor_pos = []
        self.panel.cursor_callback = self.onCursor
        self.window = int(window)
        self.symname = '%s.img%i' % (MODNAME, self.window)
        self.larch = larch
        symtable = ensuremod(self.larch)
        if symtable is not None:
            symtable.set_symbol(self.symname, self)
        if self.window not in IMG_DISPLAYS:
            IMG_DISPLAYS[self.window] = self

    def onExit(self, o, **kw):
        #print 'ImageDisplay Exit ', self.symname, o, kw
        #for k, v in IMG_DISPLAYS.items():
        #    print 'IMG DISP: ',  k, v
        try:
            symtable = self.larch.symtable
            symtable.has_group(MODNAME), self.symname
            if symtable.has_group(MODNAME):
                symtable.del_symbol(self.symname)
        except:
            pass
        if self.window in IMG_DISPLAYS:
            IMG_DISPLAYS.pop(self.window)
        self.Destroy()

    def onCursor(self,x=None, y=None, ix=None, iy=None,
                 val=None, **kw):
        symtable = ensuremod(self.larch)
        if symtable is None:
            return
        if x is not None:
            symtable.set_symbol('%s_x' % self.symname, x)
        if y is not None:
            symtable.set_symbol('%s_y' % self.symname, y)
        if ix is not None:
            symtable.set_symbol('%s_ix' % self.symname, ix)
        if iy is not None:
            symtable.set_symbol('%s_iy' % self.symname, iy)
        if val is not None:
            symtable.set_symbol('%s_val' % self.symname, val)


def _getDisplay(win=1, larch=None, wxparent=None, image=False):
    """make a plotter"""
    # global PLOT_DISPLAYS, IMG_DISPlAYS
    if larch is None:
        #print("Could not find larch?")
        return
    win = max(1, int(abs(win)))
    title   = 'Larch Plot Display Window %i' % win
    symname = '%s.plot%i' % (MODNAME, win)
    creator = PlotDisplay
    display_dict = PLOT_DISPLAYS
    if image:
        creator = ImageDisplay
        display_dict = IMG_DISPLAYS
        title   = 'Larch Image Display Window %i' % win
        symname = '%s.img%i' % (MODNAME, win)

    if win in display_dict:
        display = display_dict[win]
    else:
        display = larch.symtable.get_symbol(symname, create=True)

    if display is None:
        display = creator(window=win, wxparent=wxparent, larch=larch)
        larch.symtable.set_symbol(symname, display)

    if display is not None:
        display.SetTitle(title)
    return display

def _plot(x,y, win=1, larch=None, wxparent=None, **kws):
    """plot(x, y[, win=1], options])

    Plot 2-D trace of x, y arrays in a Plot Frame, clearing any plot currently in the Plot Frame.

    Parameters:
    --------------
        x :  array of ordinate values
        y :  array of abscissa values (x and y must be same size!)

        win: index of Plot Frame (0, 1, etc).  May create a new Plot Frame.

        label: label for trace
        title:  title for Plot
        xlabel: x-axis label
        ylabel: y-axis label
        ylog_scale: whether to show y-axis as log-scale (True or False)
        grid: whether to draw background grid (True or False)

        color: color for trace (name such as 'red', or '#RRGGBB' hex string)
        style: trace linestyle (one of 'solid', 'dashed', 'dotted', 'dot-dash')
        linewidth:  integer width of line
        marker:  symbol to draw at eac point ('+', 'o', 'x', 'square', etc)
        markersize: integer size of marker

        drawstyle: ?

        dy: array for error bars in y (must be same size as y!)
        yaxis='left'??
        use_dates

    See Also:
    ---------

        oplot

    """
    plotter = _getDisplay(wxparent=wxparent, win=win, larch=larch)

    wx.CallAfter(plotter.Raise)
    if plotter is not None:
        plotter.plot(x, y, **kws)

def _oplot(x,y, win=1, larch=None, wxparent=None, **kws):
    """oplot(x, y[, win=0], options])

    Plot 2-D trace of x, y arrays in a Plot Frame, over-plotting any plot currently in the Plot Frame.

    See Also:
    -----------
    plot

    """
    plotter = _getDisplay(wxparent=wxparent, win=win, larch=larch)

    if plotter is not None:
        plotter.oplot(x, y, **kws)
    else:
        print 'dont have plotter yet?'

def _imshow(map, win=1, larch=None, wxparent=None, **kws):
    """imshow(map[, options])

    Display an image for a 2-D array, as a map

    map: 2-dimensional array for map
    """
    img = _getDisplay(wxparent=wxparent, win=win, larch=larch, image=True)

    if img is not None:
        img.display(map, **kws)

def registerLarchPlugin():
    return (MODNAME, {'plot':_plot,
                      'oplot': _oplot,
                      'imshow':_imshow} )