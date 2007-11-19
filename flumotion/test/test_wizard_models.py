import unittest

from flumotion.common import testsuite
from flumotion.wizard.models import Flow, Component, ComponentError

class TestFlow(testsuite.TestCase):
    def setUp(self):
        self.flow = Flow()

    def testAddComponent(self):
        self.assertRaises(TypeError, self.flow.addComponent, None)

        component = Component()
        self.flow.addComponent(component)
        self.assertRaises(ComponentError, self.flow.addComponent, component)

        self.assertEqual(component.name, "component")

        component2 = Component()
        self.flow.addComponent(component2)
        self.assertEqual(component2.name, "component-2")

    def testRemoveComponent(self):
        self.assertRaises(TypeError, self.flow.removeComponent, None)

        component = Component()
        self.assertRaises(ComponentError, self.flow.removeComponent, component)

        self.flow.addComponent(component)
        self.failUnless(component.name)

        self.flow.removeComponent(component)
        self.failIf(component.name)

    def testContains(self):
        component = Component()
        self.failIf(component in self.flow)
        self.flow.addComponent(component)
        self.failUnless(component in self.flow)
        self.flow.removeComponent(component)
        self.failIf(component in self.flow)

    def testIter(self):
        component = Component()
        self.assertEquals(list(self.flow), [])
        self.flow.addComponent(component)
        self.assertEquals(list(self.flow), [component])

        component2 = Component()
        self.flow.addComponent(component2)

        self.assertEquals(list(self.flow), [component, component2])
        for component in list(self.flow):
            self.flow.removeComponent(component)
        self.assertEquals(list(self.flow), [])


class TestComponent(testsuite.TestCase):
    def setUp(self):
        self.component = Component()

if __name__ == "__main__":
    unittest.main()